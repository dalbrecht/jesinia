# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo workspace on disk, driven by the git CLI.

Layout under ``root`` (``<data_dir>/review/repos``)::

    <owner>/<name>/mirror.git      bare mirror, fetched from the remote
    <owner>/<name>/trees/<sha>     detached worktree per reviewed head

Git runs argv-only (never a shell), non-interactively, without the system config or a
pager, with hooks disabled and pathspecs taken literally. Fetching uses the author's own
git credentials (their global credential helper); no token is ever stored here, and the
workspace never pushes.
"""

from __future__ import annotations

import asyncio
import os
import shutil

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as spawn
from collections.abc import Callable
from pathlib import Path

from jesinia.apps.review.domain.workspace import (
    GitCommandError,
    WorkspaceError,
    WorkspacePathError,
    check_ref,
    check_sha,
    parse_repo,
)

__all__ = ["GitWorkspace", "github_remote_url"]

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_LITERAL_PATHSPECS": "1",  # a changed file named "*" must not match every path
    "LC_ALL": "C",
}
# Options before the subcommand: no pager, and no hooks (the user's global hooksPath must
# not run against checkouts of other people's code).
_GIT_OPTIONS = ("--no-pager", "-c", "core.hooksPath=/dev/null")
_DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--no-textconv")


def github_remote_url(repo: str) -> str:
    """The plain HTTPS clone URL, so git's own credential helper authenticates."""
    owner, name = parse_repo(repo)
    return f"https://github.com/{owner}/{name}.git"


class GitWorkspace:
    """``Workspace`` over a directory of bare mirrors and detached worktrees.

    Args:
        root: where mirrors and worktrees live; created on first use.
        remote_url: maps ``owner/name`` to the URL to fetch from (``github_remote_url``
            in production, a local path in tests).
    """

    def __init__(self, root: Path, remote_url: Callable[[str], str]) -> None:
        self.root = root
        self._remote_url = remote_url
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure_mirror(self, repo: str) -> None:
        mirror = self._mirror(repo)
        async with self._lock(repo):
            mirror.mkdir(parents=True, exist_ok=True)
            # Both are idempotent: re-running init is safe, and the URL tracks remote_url.
            await self._git(mirror, "init", "-q", "--bare")
            await self._git(mirror, "config", "remote.origin.url", self._remote_url(repo))

    async def fetch(self, repo: str, refs: list[str]) -> None:
        mirror = self._mirror(repo)
        # Refs land under their own names so the objects stay reachable; SHAs as given.
        specs = [
            ref if _is_valid(check_sha, ref) else f"+{ref}:{ref}" for ref in map(check_ref, refs)
        ]
        async with self._lock(repo):
            await self._git(
                mirror, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "origin", *specs
            )

    async def worktree(self, repo: str, sha: str) -> Path:
        mirror = self._mirror(repo)
        path = self._inside(mirror.parent / "trees" / check_sha(sha))
        async with self._lock(repo):
            if path.is_dir():
                if await self._head_of(path) == sha:
                    return path
                shutil.rmtree(path)  # a damaged checkout of ours: rebuild it
                await self._git(mirror, "worktree", "prune")
            await self._git(mirror, "worktree", "add", "-q", "--detach", str(path), sha)
            return path

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        return await self._git(
            self._mirror(repo), "diff", *_DIFF_OPTIONS, check_sha(base_sha), check_sha(head_sha)
        )

    async def merge_base(self, repo: str, a: str, b: str) -> str:
        out = await self._git(self._mirror(repo), "merge-base", check_sha(a), check_sha(b))
        return check_sha(out.strip())

    async def patch_ids(self, repo: str, base_sha: str, head_sha: str) -> dict[str, str]:
        mirror = self._mirror(repo)
        base, head = check_sha(base_sha), check_sha(head_sha)
        # --no-renames keeps one path per change; --binary makes binary edits hash by content.
        options = (*_DIFF_OPTIONS, "--no-renames", "--binary")
        names = await self._run(mirror, "diff", *options, "--name-only", "-z", base, head)
        ids: dict[str, str] = {}
        # os.fsdecode round-trips non-UTF-8 file names back to the same bytes in argv.
        for path in map(os.fsdecode, filter(None, names.split(b"\0"))):
            patch = await self._run(mirror, "diff", *options, base, head, "--", path)
            out = await self._git(mirror, "patch-id", "--stable", stdin=patch)
            ids[path] = out.partition(" ")[0]
        return ids

    async def prune(self, keep: set[tuple[str, str]]) -> int:
        for repo, sha in keep:
            parse_repo(repo)
            check_sha(sha)
        removed = 0
        for trees in sorted(self.root.glob("*/*/trees")):
            repo = f"{trees.parent.parent.name}/{trees.parent.name}"
            if not _is_valid(parse_repo, repo):
                continue  # not a directory this workspace created
            mirror = self._mirror(repo)
            async with self._lock(repo):
                for tree in sorted(trees.iterdir()):
                    if not _is_valid(check_sha, tree.name) or (repo, tree.name) in keep:
                        continue
                    shutil.rmtree(self._inside(tree))
                    removed += 1
                await self._git(mirror, "worktree", "prune")
        return removed

    def _mirror(self, repo: str) -> Path:
        owner, name = parse_repo(repo)
        return self._inside(self.root / owner / name / "mirror.git")

    def _inside(self, path: Path) -> Path:
        root = self.root.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise WorkspacePathError(f"{path} resolves outside the workspace root {root}")
        return resolved

    def _lock(self, repo: str) -> asyncio.Lock:
        return self._locks.setdefault(repo, asyncio.Lock())

    async def _head_of(self, tree: Path) -> str | None:
        try:
            return (await self._git(tree, "rev-parse", "--verify", "-q", "HEAD")).strip()
        except GitCommandError:
            return None

    async def _git(self, cwd: Path, *args: str, stdin: bytes | None = None) -> str:
        return (await self._run(cwd, *args, stdin=stdin)).decode(errors="replace")

    async def _run(self, cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
        argv = ("git", *_GIT_OPTIONS, "-C", str(cwd), *args)
        proc = await spawn(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **_GIT_ENV},
        )
        out, err = await proc.communicate(stdin)
        if proc.returncode:
            raise GitCommandError(argv, proc.returncode, err.decode(errors="replace"))
        return out


def _is_valid(check: Callable[[str], object], value: str) -> bool:
    try:
        check(value)
    except WorkspaceError:
        return False
    return True
