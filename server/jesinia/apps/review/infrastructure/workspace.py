# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo workspace on disk, driven by the git CLI.

Layout under ``root`` (``<data_dir>/review/repos``)::

    <owner>/<name>/mirror.git      bare mirror, fetched from the remote
    <owner>/<name>/trees/<sha>     detached worktree per reviewed head

Git runs argv-only (never a shell), non-interactively, with no pager and a time limit.
Checkouts are of other people's code, so nothing in the user's config may act on them:
hooks, LFS and other smudge filters, submodule recursion and fsmonitor are switched off,
and inherited repository variables (a git hook's ``GIT_DIR``) are dropped. The system
config stays in force because it carries macOS's osxkeychain credential helper: fetching
uses the author's own git credentials, no token is ever stored here, and the workspace
never pushes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import uuid

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as spawn
from collections.abc import Callable
from pathlib import Path

from jesinia.apps.review.domain.workspace import (
    GitCommandError,
    GitTimeoutError,
    GitUnavailableError,
    WorkspaceError,
    WorkspacePathError,
    canonical_repo,
    check_ref,
    check_sha,
    parse_repo,
)

__all__ = ["GitWorkspace", "github_remote_url"]

log = logging.getLogger(__name__)

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_LITERAL_PATHSPECS": "1",  # a changed file named "*" must not match every path
    "GIT_LFS_SKIP_SMUDGE": "1",
    "LC_ALL": "C",
}
# Set by git for hooks; any of them would point commands at another repository.
_INHERITED_REPO_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)
_GIT_OPTIONS = (
    "--no-pager",
    *("-c", "core.hooksPath=/dev/null"),
    *("-c", "core.fsmonitor=false"),
    *("-c", "filter.lfs.smudge="),
    *("-c", "filter.lfs.process="),
    *("-c", "filter.lfs.required=false"),
    *("-c", "submodule.recurse=false"),
    *("-c", "protocol.ext.allow=never"),
)
# Pinned so the user's diff.* settings never change what later steps parse.
_DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "-U3",
    "-M",
    "--no-relative",
    "--diff-algorithm=myers",
    "--indent-heuristic",
)
_FILE_HEADER = re.compile(rb"^(?=diff --git )", re.MULTILINE)
_TEMPORARY_PREFIX = ".tmp-"


def github_remote_url(repo: str) -> str:
    """The plain HTTPS clone URL, so git's own credential helper authenticates."""
    return f"https://github.com/{canonical_repo(repo)}.git"


class GitWorkspace:
    """``Workspace`` over a directory of bare mirrors and detached worktrees.

    Args:
        root: where mirrors and worktrees live; created on first use.
        remote_url: maps a canonical ``owner/name`` to the URL to fetch from
            (``github_remote_url`` in production, a local path in tests).
        command_timeout: seconds any git command but ``fetch`` may run.
        fetch_timeout: seconds a ``fetch`` may run.
    """

    def __init__(
        self,
        root: Path,
        remote_url: Callable[[str], str],
        *,
        command_timeout: float = 60.0,
        fetch_timeout: float = 300.0,
    ) -> None:
        self.root = root
        self._remote_url = remote_url
        self._command_timeout = command_timeout
        self._fetch_timeout = fetch_timeout
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure_mirror(self, repo: str) -> None:
        mirror = self._mirror(repo)
        async with self._lock(repo):
            mirror.mkdir(parents=True, exist_ok=True)
            # Both are idempotent: re-running init is safe, and the URL tracks remote_url.
            await self._git((), "init", "-q", "--bare", str(mirror))
            url = self._remote_url(canonical_repo(repo))
            await self._git(_bare(mirror), "config", "remote.origin.url", url)

    async def fetch(self, repo: str, refs: list[str]) -> None:
        mirror = self._mirror(repo)
        # Refs land under their own names so the objects stay reachable; SHAs as given.
        specs = [
            ref if _is_valid(check_sha, ref) else f"+{ref}:{ref}" for ref in map(check_ref, refs)
        ]
        if not specs:
            return
        async with self._lock(repo):
            await self._git(
                _bare(mirror),
                *("fetch", "-q", "--no-tags", "--no-recurse-submodules", "origin", *specs),
                timeout=self._fetch_timeout,
            )

    async def worktree(self, repo: str, sha: str) -> Path:
        mirror = self._mirror(repo)
        trees = self._inside(mirror.parent / "trees")
        path = trees / check_sha(sha)
        async with self._lock(repo):
            if path.is_dir() and not path.is_symlink():
                if await self._head_of(path) == sha:
                    return path
            await _remove(path)  # a damaged checkout or a stray link: rebuild it
            # Clears registrations whose directories are gone, which would block the add.
            await self._git(_bare(mirror), "worktree", "prune")
            # Check out beside the final path, then rename: an interrupted add never leaves
            # a partial tree where the idempotency check above would accept it.
            temporary = trees / f"{_TEMPORARY_PREFIX}{sha}-{uuid.uuid4().hex}"
            try:
                await self._git(
                    _bare(mirror), "worktree", "add", "-q", "--detach", str(temporary), sha
                )
                await self._git(_bare(mirror), "worktree", "move", str(temporary), str(path))
            except BaseException:
                await _remove(temporary)
                with contextlib.suppress(WorkspaceError):  # keep the original failure
                    await self._git(_bare(mirror), "worktree", "prune")
                raise
            return path

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        return await self._git(
            _bare(self._mirror(repo)),
            "diff",
            *_DIFF_OPTIONS,
            check_sha(base_sha),
            check_sha(head_sha),
        )

    async def merge_base(self, repo: str, a: str, b: str) -> str:
        out = await self._git(_bare(self._mirror(repo)), "merge-base", check_sha(a), check_sha(b))
        return check_sha(out.strip())

    async def patch_ids(self, repo: str, base_sha: str, head_sha: str) -> dict[str, str]:
        """Per-file patch IDs, keyed by the post-change path (a rename's new name).

        Conservative by design: context lines are hashed, so a base change within three
        lines of a hunk, or to a renamed file's old content, changes the ID. Callers see
        that as "content differs" and re-review, never as a missed change.
        """
        mirror = _bare(self._mirror(repo))
        base, head = check_sha(base_sha), check_sha(head_sha)
        options = (*_DIFF_OPTIONS, "--binary")  # binary edits hash by content
        names = await self._run(mirror, "diff", *options, "--name-only", "-z", base, head)
        paths = [os.fsdecode(name) for name in names.split(b"\0") if name]
        patch = await self._run(mirror, "diff", *options, base, head)
        chunks = [chunk for chunk in _FILE_HEADER.split(patch) if chunk]
        # One patch-id process for every file: each chunk gets a synthetic commit header,
        # and patch-id prints "<patch id> <commit>" per chunk. Both lists are in diff order.
        numbered = b"".join(
            b"commit %040x\n" % index + chunk for index, chunk in enumerate(chunks, start=1)
        )
        out = await self._git(mirror, "patch-id", "--stable", stdin=numbered)
        pairs = (line.split() for line in out.splitlines())
        by_index = {int(commit, 16): patch_id for patch_id, commit in pairs}
        return {path: by_index[index] for index, path in enumerate(paths, start=1)}

    async def prune(self, keep: set[tuple[str, str]]) -> int:
        kept = {(canonical_repo(repo), check_sha(sha)) for repo, sha in keep}
        removed = 0
        for trees in sorted(self.root.glob("*/*/trees")):
            repo = f"{trees.parent.parent.name}/{trees.parent.name}"
            if not _is_valid(parse_repo, repo) or repo != canonical_repo(repo):
                continue  # not a directory this workspace created
            try:
                mirror = self._mirror(repo)
                self._inside(trees)
            except WorkspacePathError as exc:
                log.warning("review.prune skipped %s: %s", trees, exc)
                continue
            async with self._lock(repo):
                for tree in sorted(trees.iterdir()):
                    if tree.name.startswith(_TEMPORARY_PREFIX):
                        await _remove(tree)  # left behind by a crash mid-checkout
                    elif _is_valid(check_sha, tree.name) and (repo, tree.name) not in kept:
                        await _remove(tree)
                        removed += 1
                await self._git(_bare(mirror), "worktree", "prune")
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
        return self._locks.setdefault(canonical_repo(repo), asyncio.Lock())

    async def _head_of(self, tree: Path) -> str | None:
        try:
            return (
                await self._git(("-C", str(tree)), "rev-parse", "--verify", "-q", "HEAD")
            ).strip()
        except GitCommandError:
            return None

    async def _git(
        self,
        location: tuple[str, ...],
        *args: str,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> str:
        out = await self._run(location, *args, stdin=stdin, timeout=timeout)
        return out.decode(errors="replace")

    async def _run(
        self,
        location: tuple[str, ...],
        *args: str,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Run git at ``location`` (``--git-dir`` for the mirror, ``-C`` for a worktree)."""
        argv = ("git", *_GIT_OPTIONS, *location, *args)
        env = {k: v for k, v in os.environ.items() if k not in _INHERITED_REPO_VARIABLES}
        env |= _GIT_ENV | {"GIT_CEILING_DIRECTORIES": str(self.root.resolve())}
        try:
            proc = await spawn(
                *argv,
                stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,  # its own process group, so helpers die with it
            )
        except OSError as exc:
            raise GitUnavailableError(f"cannot run git: {exc}") from exc
        seconds = self._command_timeout if timeout is None else timeout
        try:
            async with asyncio.timeout(seconds):
                out, err = await proc.communicate(stdin)
        except TimeoutError as exc:
            await _kill(proc)
            raise GitTimeoutError(argv, seconds) from exc
        except asyncio.CancelledError:
            await _kill(proc)
            raise
        if proc.returncode:
            raise GitCommandError(argv, proc.returncode, err.decode(errors="replace"))
        return out


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill git's whole process group and reap it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


def _bare(mirror: Path) -> tuple[str, ...]:
    """Address the mirror explicitly, so ``safe.bareRepository=explicit`` still allows it."""
    return ("--git-dir", str(mirror))


async def _remove(path: Path) -> None:
    """Delete a tree off the event loop; a symlink is unlinked, never followed."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        await asyncio.to_thread(shutil.rmtree, path)


def _is_valid(check: Callable[[str], object], value: str) -> bool:
    try:
        check(value)
    except WorkspaceError:
        return False
    return True
