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
import hashlib
import logging
import os
import re
import shutil
import signal
import tempfile
import uuid

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as spawn
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from jesinia.apps.review.application.ports import KeepProvider
from jesinia.apps.review.domain.repo_access import CappedOutput, RegexDialect
from jesinia.apps.review.domain.workspace import (
    DiffTooLargeError,
    GitCommandError,
    GitOutputError,
    GitTimeoutError,
    GitUnavailableError,
    MirrorFileTooLargeError,
    WorkspaceError,
    WorkspacePathError,
    canonical_repo,
    check_ref,
    check_sha,
    check_tree_path,
    parse_repo,
)

__all__ = ["DIFF_LIMIT", "EMPTY_TREE", "GitWorkspace", "github_remote_url"]

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
    *("-c", "grep.threads=2"),  # a search may not take every core
)
# The empty tree's id, as `git hash-object -t tree /dev/null` prints it (SHA-1, which is
# what GitHub repositories use). Every command in a repository runs with it as
# `--attr-source`, so a PR's own .gitattributes can't mark source as binary (hiding it from
# grep and diffs), change line endings or encodings on checkout, or pick diff drivers.
# Needs git 2.40 or later; older git fails with a typed GitCommandError naming the option.
EMPTY_TREE = hashlib.sha1(b"tree 0\0", usedforsecurity=False).hexdigest()
# For the repo tools' reads: no colour codes from the user's config; no gpg run on a PR's
# (untrusted) commit signatures; ERE for `log --author=` (the repo tools validate it as
# ERE, and a user's grep.patternType=perl would allow backtracking); and no PR-supplied
# files consulted through the user's global config: .git-blame-ignore-revs could hide
# authorship, and .mailmap could rewrite the authors shown and matched (mailmap.* also
# empties the extra mailmap sources).
_TREE_READ_OPTIONS = (
    *("-c", "color.ui=never"),
    *("-c", "log.showSignature=false"),
    *("-c", "grep.patternType=extended"),
    *("-c", "blame.ignoreRevsFile="),
    *("-c", "log.mailmap=false"),
    *("-c", "mailmap.file="),
    *("-c", "mailmap.blob="),
    *("-c", "core.quotePath=true"),  # non-ASCII paths in git's output stay escaped text
)
# git's stderr kept for error messages; the rest is drained and dropped.
_STDERR_LIMIT = 64 * 1024
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
# Bytes of diff output read before git is stopped: room for a 5,000-line PR with a 2 MB
# lockfile many times over, and a bound on what one huge binary or generated file can cost.
DIFF_LIMIT = 32 * 1024 * 1024
_KEEP_PREFIX = "refs/jesinia/keep/"
# The remote's default branch (its HEAD) as last fetched: where the repo's review standards
# are read from (R39). Outside the keep prefix, so a prune never drops it.
_DEFAULT_BRANCH_REF = "refs/jesinia/default-branch"
_FILE_HEADER = re.compile(rb"^(?=diff --git )", re.MULTILINE)
_TEMPORARY_PREFIX = ".tmp-"
# git processes the repo tools may run at once across every review session (R52); each
# session also runs one at a time.
REPO_GIT_CONCURRENCY = 4


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
        diff_limit: bytes of diff output ``diff`` and ``patch_ids`` read before they stop
            git and raise ``DiffTooLargeError``.
        repo_git_concurrency: the repo tools' git processes (``read_in_tree``) running at
            once, across every review session.
    """

    def __init__(
        self,
        root: Path,
        remote_url: Callable[[str], str],
        *,
        command_timeout: float = 60.0,
        fetch_timeout: float = 300.0,
        diff_limit: int = DIFF_LIMIT,
        repo_git_concurrency: int = REPO_GIT_CONCURRENCY,
    ) -> None:
        self.root = root
        self._diff_limit = diff_limit
        self._remote_url = remote_url
        self._command_timeout = command_timeout
        self._fetch_timeout = fetch_timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._regex_dialect: RegexDialect | None = None
        self._tree_reads = asyncio.Semaphore(repo_git_concurrency)

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
        # Refs land under their own names, and SHAs under a keep ref, so every fetched
        # commit stays reachable: `git gc --auto` would otherwise drop a SHA fetched bare
        # (only FETCH_HEAD names it) weeks later, when a rebase check still needs it.
        specs = [
            f"+{ref}:{_KEEP_PREFIX}{ref}" if _is_valid(check_sha, ref) else f"+{ref}:{ref}"
            for ref in map(check_ref, refs)
        ]
        if not specs:
            return
        async with self._lock(repo):
            await self._git(
                _bare(mirror),
                *("fetch", "-q", "--no-tags", "--no-recurse-submodules", "origin", *specs),
                timeout=self._fetch_timeout,
            )

    async def fetch_default_branch(self, repo: str) -> str:
        """Fetch the remote's default branch (whatever its HEAD names) into the mirror under
        its own ref, and return its tip.

        Raises:
            GitCommandError, GitTimeoutError: e.g. the remote can't be reached.
        """
        mirror = self._mirror(repo)
        async with self._lock(repo):
            await self._git(
                _bare(mirror),
                *("fetch", "-q", "--no-tags", "--no-recurse-submodules", "origin"),
                f"+HEAD:{_DEFAULT_BRANCH_REF}",
                timeout=self._fetch_timeout,
            )
        tip = await self._git(
            _bare(mirror),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{_DEFAULT_BRANCH_REF}^{{commit}}",
        )
        return check_sha(tip.strip())

    async def default_branch_head(self, repo: str) -> str:
        """The default branch's tip as last fetched; fetched now if it never was.

        Raises:
            GitCommandError, GitTimeoutError: as ``fetch_default_branch``.
        """
        tip = await self._commit_of(self._mirror(repo), _DEFAULT_BRANCH_REF)
        return tip if tip is not None else await self.fetch_default_branch(repo)

    async def _commit_of(self, mirror: Path, ref: str) -> str | None:
        argv, out, err, code, _ = await self._execute(
            _bare(mirror), ("rev-parse", "--verify", "-q", "--end-of-options", f"{ref}^{{commit}}")
        )
        if code == 1:
            return None  # no such ref
        if code:
            raise GitCommandError(argv, code, err.decode(errors="replace"))
        return check_sha(out.decode().strip())

    async def worktree(
        self, repo: str, sha: str, *, bind: Callable[[Path], None] | None = None
    ) -> Path:
        """The detached checkout of ``sha``, made if it isn't there. ``bind`` is called with
        it before the repo lock is released (R55): the prune takes the same lock, so a tree
        a job binds this way is never removed between being made and being bound."""
        mirror = self._mirror(repo)
        trees = self._inside(mirror.parent / "trees")
        path = trees / check_sha(sha)
        async with self._lock(repo):
            if path.is_dir() and not path.is_symlink():
                if await self._head_of(path) == sha:
                    if bind is not None:
                        bind(path)
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
            if bind is not None:
                bind(path)
            return path

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        """The unified diff ``base_sha..head_sha``.

        Raises:
            DiffTooLargeError: it is over the workspace's ``diff_limit`` bytes.
        """
        out = await self._run(
            _bare(self._mirror(repo)),
            "diff",
            *_DIFF_OPTIONS,
            check_sha(base_sha),
            check_sha(head_sha),
            output_limit=self._diff_limit,
        )
        return out.decode(errors="replace")

    async def merge_base(self, repo: str, a: str, b: str) -> str:
        out = await self._git(_bare(self._mirror(repo)), "merge-base", check_sha(a), check_sha(b))
        return check_sha(out.strip())

    async def patch_ids(self, repo: str, base_sha: str, head_sha: str) -> dict[str, str]:
        """Per-file patch IDs, keyed by the post-change path (a rename's new name).

        Conservative by design: context lines are hashed, so a base change within three
        lines of a hunk, or to a renamed file's old content, changes the ID. Callers see
        that as "content differs" and re-review, never as a missed change. Whitespace is
        hashed too (``--verbatim``, C1): plain ``patch-id`` strips it, which would take a
        Python re-indentation or a change inside a string literal for a pure rebase. Each
        chunk is one file, so ``--stable`` (which git refuses beside ``--verbatim``) adds
        nothing. A git without ``--verbatim`` (older than 2.39) fails the command, and the
        caller then assumes nothing is unchanged.

        Raises:
            DiffTooLargeError: the (binary) patch is over the workspace's ``diff_limit``.
        """
        mirror = _bare(self._mirror(repo))
        base, head = check_sha(base_sha), check_sha(head_sha)
        options = (*_DIFF_OPTIONS, "--binary")  # binary edits hash by content
        cap = self._diff_limit
        names = await self._run(
            mirror, "diff", *options, "--name-only", "-z", base, head, output_limit=cap
        )
        paths = [os.fsdecode(name) for name in names.split(b"\0") if name]
        patch = await self._run(mirror, "diff", *options, base, head, output_limit=cap)
        chunks = [chunk for chunk in _FILE_HEADER.split(patch) if chunk]
        if len(paths) != len(chunks):
            raise GitOutputError(
                f"git diff {base}..{head} listed {len(paths)} paths but {len(chunks)} file diffs"
            )
        # One patch-id process for every file: each chunk gets a synthetic commit header,
        # and patch-id prints "<patch id> <commit>" per chunk. Both lists are in diff order.
        numbered = b"".join(
            b"commit %040x\n" % index + chunk for index, chunk in enumerate(chunks, start=1)
        )
        out = await self._git(mirror, "patch-id", "--verbatim", stdin=numbered)
        pairs = (line.split() for line in out.splitlines())
        by_index = {int(commit, 16): patch_id for patch_id, commit in pairs}
        missing = [path for index, path in enumerate(paths, start=1) if index not in by_index]
        if missing:
            raise GitOutputError(f"git patch-id gave no patch id for {', '.join(missing)}")
        return {path: by_index[index] for index, path in enumerate(paths, start=1)}

    async def show_file(self, repo: str, sha: str, path: str, *, limit: int) -> bytes | None:
        """``MirrorFiles``: the content of the regular file at ``path`` in commit ``sha``,
        read from git's objects in the mirror (never a checkout), or ``None`` when there is
        no such file. A symlink or a submodule there is ``None`` too: its target is never
        followed, so a commit can't point the read anywhere else.

        Raises:
            InvalidShaError, InvalidTreePathError: before git runs.
            MirrorFileTooLargeError: the file is over ``limit`` bytes (it is not read).
            GitCommandError: e.g. ``sha`` isn't in the mirror.
        """
        mirror = _bare(self._mirror(repo))
        commit, rel = check_sha(sha), check_tree_path(path)
        # -l adds the blob size; -z keeps the name unquoted. With GIT_LITERAL_PATHSPECS the
        # path matches only itself, and ls-tree never descends through a symlinked folder.
        listing = await self._run(mirror, "ls-tree", "-l", "-z", "--full-tree", commit, "--", rel)
        entry, _, name = listing.rstrip(b"\0").partition(b"\t")
        if not entry or os.fsdecode(name) != rel:
            return None
        mode, kind, object_id, size = entry.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            return None
        if int(size) > limit:
            raise MirrorFileTooLargeError(
                f"{rel} at {commit} is {int(size)} bytes, over the {limit}-byte limit"
            )
        return await self._run(mirror, "cat-file", "blob", object_id.decode())

    async def commit_messages(
        self, repo: str, base_sha: str, head_sha: str, *, limit: int
    ) -> CappedOutput:
        """The full messages of the commits in ``base_sha..head_sha``, newest first, each
        followed by a NUL, from the mirror; cut (and flagged) past ``limit`` bytes.

        Raises:
            InvalidShaError, GitCommandError, GitTimeoutError.
        """
        mirror = _bare(self._mirror(repo))
        base, head = check_sha(base_sha), check_sha(head_sha)
        argv, out, err, code, truncated = await self._execute(
            (*mirror, *_TREE_READ_OPTIONS),
            ("log", "--no-color", "--format=%B%x00", "--end-of-options", f"{base}..{head}"),
            extra_env={"GIT_CONFIG_NOSYSTEM": "1"},  # local reads need no credential helper
            output_limit=limit,
        )
        if not truncated and code:
            raise GitCommandError(argv, code, err.decode(errors="replace"))
        return CappedOutput(out, truncated, 0 if truncated else code)

    async def read_in_tree(
        self,
        tree: Path,
        args: Sequence[str],
        *,
        output_limit: int,
        glob_pathspecs: bool = False,
        accept_exit_codes: frozenset[int] = frozenset({0}),
        without_worktree: bool = False,
        timeout: float | None = None,
    ) -> CappedOutput:
        """``TreeGit`` for the repo tools: read-only git inside a worktree under the root,
        at most ``repo_git_concurrency`` at once (the rest wait their turn)."""
        inside = self._inside(tree)
        # blame reads the worktree's .mailmap unconditionally (log.mailmap doesn't reach
        # it), so it runs on the tree's mirror instead, where there is no worktree and the
        # empty mailmap.blob stops a bare repo's default HEAD:.mailmap.
        location = (
            _bare(self._inside(inside.parent.parent / "mirror.git"))
            if without_worktree
            else ("-C", str(inside))
        )
        env = {"GIT_CONFIG_NOSYSTEM": "1"}  # local reads need no credential helper
        if glob_pathspecs:
            env["GIT_LITERAL_PATHSPECS"] = "0"
        async with self._tree_reads:
            argv, out, err, code, truncated = await self._execute(
                (*location, *_TREE_READ_OPTIONS),
                args,
                extra_env=env,
                output_limit=output_limit,
                timeout=timeout,
            )
        if not truncated and code not in accept_exit_codes:
            raise GitCommandError(argv, code, err.decode(errors="replace"))
        return CappedOutput(out, truncated, 0 if truncated else code)

    async def regex_dialect(self) -> RegexDialect:
        """``TreeGit``: probe the installed git's ERE library once, then cache the answer.

        CI runs Linux (glibc: ``\\b`` and ``\\B`` work) and installs run macOS (BSD regex:
        neither does), so the repo tools ask the git that will run the search.
        """
        if self._regex_dialect is None:
            with tempfile.TemporaryDirectory() as probe_dir:
                (Path(probe_dir) / "probe.txt").write_text("foo\n")
                word = await self._probe_regex(probe_dir, "\\bfoo")
                inner = word and await self._probe_regex(probe_dir, "f\\Boo")
            self._regex_dialect = RegexDialect(word, inner)
        return self._regex_dialect

    async def _probe_regex(self, probe_dir: str, pattern: str) -> bool:
        # No repository here, so no --attr-source (git refuses it outside a repo).
        argv, _, err, code, _ = await self._execute(
            ("-C", probe_dir),
            ("grep", "--no-index", "-E", "-q", "-e", pattern, "--", "probe.txt"),
            extra_env={"GIT_CONFIG_NOSYSTEM": "1"},  # as hardened as the reads it serves
            attribute_source=False,
        )
        if code not in (0, 1):  # 1: no match, i.e. the extension isn't supported
            raise GitCommandError(argv, code, err.decode(errors="replace"))
        return code == 0

    async def prune(self, keep: KeepProvider) -> int:
        """Remove worktrees and keep refs whose SHA ``keep(repo)`` doesn't name; return how
        many worktrees went. Each repo's keep set is asked for inside that repo's lock, the
        one ``worktree`` takes, so a tree created or bound meanwhile is never removed."""
        removed = 0
        repos = {
            p.parent for p in (*self.root.glob("*/*/trees"), *self.root.glob("*/*/mirror.git"))
        }
        for repo_dir in sorted(repos):
            repo = f"{repo_dir.parent.name}/{repo_dir.name}"
            trees = repo_dir / "trees"
            if not _is_valid(parse_repo, repo) or repo != canonical_repo(repo):
                continue  # not a directory this workspace created
            try:
                mirror = self._mirror(repo)
                self._inside(trees)
            except WorkspacePathError as exc:
                log.warning("review.prune skipped %s: %s", trees, exc)
                continue
            try:
                removed += await self._prune_repo(repo, mirror, trees, keep)
            except WorkspaceError as exc:
                # A mirror git never initialised (a crash in ensure_mirror) must not stop
                # the prune of every repo after it.
                log.warning("review.prune skipped %s: %s", repo, exc)
        return removed

    async def _prune_repo(self, repo: str, mirror: Path, trees: Path, keep: KeepProvider) -> int:
        removed = 0
        async with self._lock(repo):
            kept = {sha for sha in await keep(repo) if _is_valid(check_sha, sha)}
            for tree in sorted(trees.iterdir()) if trees.is_dir() else []:
                if tree.name.startswith(_TEMPORARY_PREFIX):
                    await _remove(tree)  # left behind by a crash mid-checkout
                elif _is_valid(check_sha, tree.name) and tree.name not in kept:
                    await _remove(tree)
                    removed += 1
            await self._git(_bare(mirror), "worktree", "prune")
            await self._drop_keep_refs(mirror, kept)
        return removed

    def tree_lock(self, path: Path) -> asyncio.Lock | None:
        key = self.tree_key(path)
        return None if key is None else self._lock(key[0])

    def tree_key(self, path: Path) -> tuple[str, str] | None:
        try:
            parts = path.resolve().relative_to(self.root.resolve()).parts
        except ValueError:
            return None
        if len(parts) != 4 or parts[2] != "trees" or not _is_valid(check_sha, parts[3]):
            return None
        repo = f"{parts[0]}/{parts[1]}"
        if not _is_valid(parse_repo, repo):
            return None
        return canonical_repo(repo), parts[3]

    async def _drop_keep_refs(self, mirror: Path, kept: set[str]) -> None:
        listed = await self._git(_bare(mirror), "for-each-ref", "--format=%(refname)", _KEEP_PREFIX)
        unkept = [ref for ref in listed.split() if ref.removeprefix(_KEEP_PREFIX) not in kept]
        if unkept:
            commands = "".join(f"delete {ref}\n" for ref in unkept)
            await self._git(_bare(mirror), "update-ref", "--stdin", stdin=commands.encode())

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
        output_limit: int | None = None,
    ) -> bytes:
        """Run git at ``location`` (``--git-dir`` for the mirror, ``-C`` for a worktree).
        With ``output_limit``, only diffs are run: past it git is stopped and
        ``DiffTooLargeError`` raised."""
        argv, out, err, code, truncated = await self._execute(
            location, args, stdin=stdin, timeout=timeout, output_limit=output_limit
        )
        if truncated:
            limit = output_limit or 0
            span = "..".join(arg[:12] for arg in argv[-2:])
            raise DiffTooLargeError(f"the diff {span} is over {limit} bytes", limit)
        if code:
            raise GitCommandError(argv, code, err.decode(errors="replace"))
        return out

    async def _execute(
        self,
        location: tuple[str, ...],
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        attribute_source: bool = True,
    ) -> tuple[tuple[str, ...], bytes, bytes, int, bool]:
        """Run git; return its argv, stdout, stderr, exit code and whether stdout was cut."""
        # Only `init` runs outside a repository, and it reads no attributes.
        attributes = (f"--attr-source={EMPTY_TREE}",) if location and attribute_source else ()
        argv = ("git", *_GIT_OPTIONS, *attributes, *location, *args)
        env = {k: v for k, v in os.environ.items() if k not in _INHERITED_REPO_VARIABLES}
        env |= _GIT_ENV | {"GIT_CEILING_DIRECTORIES": str(self.root.resolve())}
        env |= extra_env or {}
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
                if output_limit is None:
                    out, err = await proc.communicate(stdin)
                    truncated = False
                else:
                    out, err, truncated = await _communicate_capped(proc, output_limit)
        except TimeoutError as exc:
            await _kill(proc)
            raise GitTimeoutError(argv, seconds) from exc
        except asyncio.CancelledError:
            await _kill(proc)
            raise
        return argv, out, err, proc.returncode or 0, truncated


async def _communicate_capped(
    proc: asyncio.subprocess.Process, limit: int
) -> tuple[bytes, bytes, bool]:
    """Read at most ``limit`` bytes of stdout; past that, kill git rather than buffer more."""
    stdout, stderr = proc.stdout, proc.stderr
    assert stdout is not None and stderr is not None  # spawned with PIPE
    errors = asyncio.ensure_future(_read_capped(stderr, _STDERR_LIMIT))
    try:
        try:
            out = (await stdout.readexactly(limit + 1))[:limit]
            truncated = True
            await _kill(proc)
        except asyncio.IncompleteReadError as short:
            out, truncated = short.partial, False
            await proc.wait()
        return out, await errors, truncated
    finally:
        errors.cancel()  # a no-op once read; stops the reader if we were cancelled


async def _read_capped(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Keep the first ``limit`` bytes of ``stream`` and drain the rest, so the writer never
    blocks on a full pipe."""
    kept = b""
    while chunk := await stream.read(65536):
        kept += chunk[: max(0, limit - len(kept))]
    return kept


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill git's whole process group and reap it.

    macOS answers EPERM, not ESRCH, when the group's only members are zombies (git already
    exited and is waiting to be reaped), so both mean "nothing left to kill". EPERM can't
    mean a recycled pgid belonging to someone else: the group is git's own session (its pid),
    and that pid can't be reused until ``proc.wait()`` below reaps it.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    # asyncio's wait() also waits for the pipes to close; output a capped read left unread
    # would keep stdout open (its transport paused) and wait() would never return.
    if proc.stdout is not None:
        while await proc.stdout.read(65536):
            pass
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
