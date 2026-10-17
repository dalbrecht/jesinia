# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo tools: how review agents read a PR's code, and only that PR's code.

Each call is resolved to the worktree bound to the caller's review session. Paths must
resolve inside it (symlinks included); files the coverage rules skip are refused; every
result is capped and says what its caps are. Searching and history go through git
(``TreeGit``), which reads git objects rather than following symlinks on disk.
"""

from __future__ import annotations

import asyncio
import os
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import BinaryIO, NotRequired, TypedDict

from jesinia.apps.review.application.ports import TreeGit
from jesinia.apps.review.domain.repo_access import (
    FILE_LINES_PER_SESSION,
    GIT_MAX_BYTES,
    GIT_MAX_LINES,
    GREP_MAX_BYTES,
    GREP_MAX_LINE_CHARS,
    GREP_MAX_MATCHES,
    GREP_RAW_BYTES,
    LS_MAX_ENTRIES,
    READ_LINES_PER_CALL,
    READ_MAX_BYTES,
    AllowanceExhaustedError,
    AutoSkippedFileError,
    CappedOutput,
    FileLineAllowance,
    InvalidReadRangeError,
    NoReviewSessionError,
    NotACommitError,
    RepoGitError,
    RepoPathNotFoundError,
    UnsafePathError,
    check_glob,
    check_pattern,
    is_plain_text,
    normalize_relative_path,
    plan_git_command,
    posix_ere,
)
from jesinia.apps.review.domain.skip import auto_skip_reason, directory_skip_reason
from jesinia.apps.review.domain.workspace import GitCommandError, WorkspaceError

# git's own test: a NUL byte in the first 8000 bytes means binary.
_BINARY_SNIFF_BYTES = 8000


class ReadResult(TypedDict):
    path: str
    start: int
    end: int
    text: str
    has_more: bool
    truncated: bool
    allowance_remaining: int
    cap: str


class GrepMatch(TypedDict):
    path: str
    line: int
    text: str
    line_truncated: bool


class GrepResult(TypedDict):
    pattern: str
    glob: str | None
    matches: list[GrepMatch]
    truncated: bool
    withheld_by_allowance: int
    skipped_files: list[str]
    cap: str


class LsEntry(TypedDict):
    name: str
    kind: str  # "file", "dir" or "symlink"
    size: NotRequired[int]  # files
    skip: NotRequired[str | None]  # files and dirs: the auto-skip reason
    inside: NotRequired[bool]  # symlinks: whether the target stays inside the PR


class LsResult(TypedDict):
    path: str
    entries: list[LsEntry]
    truncated: bool
    cap: str


class GitResult(TypedDict):
    cmd: str
    output: str
    truncated: bool
    cap: str


def safe_path(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root``, following symlinks, and refuse anything that leaves it.

    Raises:
        UnsafePathError: absolute paths, ``..``, ``.git``, or a resolved target outside
            ``root`` or inside its ``.git`` (for example through a symlink).
    """
    base = root.resolve()
    target = (base / normalize_relative_path(rel)).resolve()
    if not target.is_relative_to(base):
        raise UnsafePathError(f"{rel!r} resolves outside the worktree")
    if any(part.lower() == ".git" for part in target.relative_to(base).parts):
        raise UnsafePathError(f"{rel!r} resolves into git's own files")
    return target


def _relative(root: Path, target: Path) -> str:
    return target.relative_to(root).as_posix()


def _is_binary(sample: bytes) -> bool:
    return b"\0" in sample[:_BINARY_SNIFF_BYTES]


def _logical_lines(handle: BinaryIO) -> Iterator[bytes]:
    """Each line of ``handle``, cut to ``READ_MAX_BYTES + 1`` bytes; the rest of an
    over-long line is skipped without ever being held in memory whole."""
    while raw := handle.readline(READ_MAX_BYTES + 1):
        if len(raw) > READ_MAX_BYTES and not raw.endswith(b"\n"):
            while (rest := handle.readline(READ_MAX_BYTES)) and not rest.endswith(b"\n"):
                pass
        yield raw


def _read_lines(path: Path, first: int, count: int) -> tuple[list[str], bool, bool]:
    """Lines ``first``.. (at most ``count``) of ``path``, whether more follow, and whether
    the byte cap stopped the read. A single line longer than the cap is cut to it."""
    lines: list[str] = []
    size = 0
    with path.open("rb") as handle:
        for number, raw in enumerate(_logical_lines(handle), start=1):
            if number < first:
                continue
            if len(lines) == count:
                return lines, True, False
            if size + len(raw) > READ_MAX_BYTES:
                if not lines:
                    lines.append(raw[:READ_MAX_BYTES].decode(errors="replace"))
                return lines, True, True
            lines.append(raw.decode(errors="replace"))
            size += len(raw)
    return lines, False, False


def _allowance_key(root: Path, rel: str) -> str:
    """The allowance key of a worktree path: the file's identity on disk, so case, Unicode
    normalization, symlink and hardlink spellings of one file share one allowance. A path
    with no file on disk (deleted in the PR) is keyed by its folded spelling."""
    try:
        stat = (root / rel).stat()
    except OSError:
        return "path:" + unicodedata.normalize("NFC", rel).casefold()
    return f"inode:{stat.st_dev}:{stat.st_ino}"


def _parse_grep(data: bytes) -> list[tuple[str, int, bytes]]:
    """``git grep -n -z`` records as ``(path, line, text)``.

    Parsed NUL-first: a path may contain a newline, but the matched text can't (it is one
    line), so each field after a line number is ``<text>\n<next path>``. A final record cut
    mid-way is dropped.

    Raises:
        RepoGitError: output that isn't in that shape (a NUL inside matched text).
    """
    fields = data.split(b"\0")
    records: list[tuple[str, int, bytes]] = []
    path = fields[0]
    for index in range(1, len(fields) - 1, 2):
        line, rest = fields[index], fields[index + 1]
        text, newline, following = rest.partition(b"\n")
        if not newline:
            break
        if not line.isdigit():
            raise RepoGitError("could not parse git grep output; narrow the search with a glob")
        records.append((os.fsdecode(path), int(line), text))
        path = following
    return records


class RepoTools:
    """``repo_read``, ``repo_grep``, ``repo_ls`` and ``repo_git`` over review worktrees.

    Args:
        sessions_to_worktree: the worktree bound to a review session, or ``None``.
        git: read-only git inside a worktree.
        allowance: per-session, per-file line allowance shared by every tool.
    """

    def __init__(
        self,
        sessions_to_worktree: Callable[[str], Path | None],
        git: TreeGit,
        allowance: FileLineAllowance,
    ) -> None:
        self._sessions_to_worktree = sessions_to_worktree
        self._git = git
        self._allowance = allowance
        self._locks: dict[str, asyncio.Lock] = {}

    async def read(
        self, session_id: str | None, path: str, start: int | None = None, end: int | None = None
    ) -> ReadResult:
        """Lines ``start``..``end`` (1-based, inclusive) of a file, numbered ``<n>\\t<line>``.

        Raises:
            NoReviewSessionError, UnsafePathError, RepoPathNotFoundError,
            InvalidReadRangeError, AutoSkippedFileError, AllowanceExhaustedError.
        """
        session, root = self._session(session_id)
        target = safe_path(root, path)
        rel = _relative(root, target)
        if not target.is_file():
            raise RepoPathNotFoundError(f"no file at {path!r}")
        first = 1 if start is None else start
        if first < 1:
            raise InvalidReadRangeError("start is a 1-based line number")
        last = first + READ_LINES_PER_CALL - 1 if end is None else end
        if last < first:
            raise InvalidReadRangeError("end must not be before start")
        with target.open("rb") as handle:
            sample = handle.read(_BINARY_SNIFF_BYTES)
        self._refuse_skipped(rel, _is_binary(sample))
        key = _allowance_key(root, rel)
        wanted = min(last - first + 1, READ_LINES_PER_CALL)
        granted = await self._reserve(session, key, rel, wanted)
        lines: list[str] = []
        try:
            lines, has_more, byte_capped = await asyncio.to_thread(
                _read_lines, target, first, granted
            )
        finally:
            self._allowance.refund(session, key, granted - len(lines))
        truncated = has_more and len(lines) < last - first + 1
        remaining = self._allowance.remaining(session, key)
        cap = (
            f"repo_read returns at most {READ_LINES_PER_CALL} lines per call (and at most "
            f"{READ_MAX_BYTES // 1024} KB), and {FILE_LINES_PER_SESSION} lines per file per "
            f"review session; {remaining} lines of {rel} remain in this session's allowance."
        )
        if truncated and byte_capped:
            cap += f" This read stopped at {READ_MAX_BYTES} bytes."
        elif truncated and granted < wanted:
            cap += " This read stopped at what remained of the per-file allowance."
        elif truncated:
            cap += " This read stopped at the per-call line cap; ask for the next range."
        return {
            "path": rel,
            "start": first,
            "end": first + len(lines) - 1,
            "text": "".join(f"{first + i}\t{line}" for i, line in enumerate(lines)),
            "has_more": has_more,
            "truncated": truncated,
            "allowance_remaining": remaining,
            "cap": cap,
        }

    async def grep(
        self, session_id: str | None, pattern: str, glob: str | None = None
    ) -> GrepResult:
        """Search the PR's tracked files: literally for plain text, else as an extended
        regex (``git grep -F`` / ``-E``).

        Binary and auto-skipped files are left out; matched lines count against each file's
        allowance.

        Raises:
            NoReviewSessionError, InvalidSearchError, RepoGitError.
        """
        session, root = self._session(session_id)
        # --cached: search the checked-out commit's blobs, never files (or symlink targets)
        # on disk; --no-column: a user's grep.column=true would change the record format.
        args = ["grep", "--cached", "-I", "-n", "-z", "--no-column", "--no-color"]
        plain = is_plain_text(check_pattern(pattern))
        args += ["--no-textconv", "-F" if plain else "-E"]
        args += ["-e", pattern if plain else posix_ere(pattern)]
        if glob is not None:
            args += ["--", check_glob(glob)]
        output = await self._run_git(
            root,
            args,
            output_limit=GREP_RAW_BYTES,
            glob_pathspecs=glob is not None,
            accept_exit_codes=frozenset({0, 1}),  # 1: nothing matched
        )
        matches: list[GrepMatch] = []
        skipped: set[str] = set()
        keys: dict[str, str] = {}
        withheld = 0
        size = 0
        truncated = output.truncated
        for rel, line, raw_text in _parse_grep(output.data):
            # repo_read refuses control characters in paths, so they are skipped here too.
            if auto_skip_reason(rel, False) is not None or any(ord(c) < 32 for c in rel):
                skipped.add(rel)
                continue
            text = raw_text.decode(errors="replace")
            entry_size = len(rel) + min(len(text), GREP_MAX_LINE_CHARS) + 16
            if len(matches) >= GREP_MAX_MATCHES or size + entry_size > GREP_MAX_BYTES:
                truncated = True
                continue
            key = keys.setdefault(rel, _allowance_key(root, rel))
            if self._allowance.take(session, key, 1) == 0:
                withheld += 1
                continue
            size += entry_size
            matches.append(
                {
                    "path": rel,
                    "line": line,
                    "text": text[:GREP_MAX_LINE_CHARS],
                    "line_truncated": len(text) > GREP_MAX_LINE_CHARS,
                }
            )
        return {
            "pattern": pattern,
            "glob": glob,
            "matches": matches,
            "truncated": truncated,
            "withheld_by_allowance": withheld,
            "skipped_files": sorted(skipped),
            "cap": (
                f"repo_grep returns at most {GREP_MAX_MATCHES} matches and "
                f"{GREP_MAX_BYTES // 1024} KB per call, each line cut to "
                f"{GREP_MAX_LINE_CHARS} characters; matched lines count against the "
                f"{FILE_LINES_PER_SESSION}-line per-file allowance, and binary, lockfile, "
                "generated and vendored files are not searched."
            ),
        }

    async def ls(self, session_id: str | None, path: str = ".") -> LsResult:
        """A directory's entries: files with size and skip reason, directories, and
        symlinks (with whether they stay inside the PR). ``.git`` is never listed.

        Raises:
            NoReviewSessionError, UnsafePathError, RepoPathNotFoundError.
        """
        _, root = self._session(session_id)
        target = safe_path(root, path)
        if not target.is_dir():
            raise RepoPathNotFoundError(f"no directory at {path!r}")
        where = _relative(root, target)
        entries: list[LsEntry] = []
        children = sorted(child for child in target.iterdir() if child.name.lower() != ".git")
        for child in children[:LS_MAX_ENTRIES]:
            rel = _relative(root, child)
            if child.is_symlink():
                entries.append({"name": child.name, "kind": "symlink", "inside": _stays(root, rel)})
            elif child.is_dir():
                entries.append(
                    {"name": child.name, "kind": "dir", "skip": directory_skip_reason(rel)}
                )
            else:
                entries.append(
                    {
                        "name": child.name,
                        "kind": "file",
                        "size": child.lstat().st_size,
                        "skip": auto_skip_reason(rel, False),
                    }
                )
        return {
            "path": where,
            "entries": entries,
            "truncated": len(children) > LS_MAX_ENTRIES,
            "cap": f"repo_ls lists at most {LS_MAX_ENTRIES} entries per directory",
        }

    async def git(self, session_id: str | None, cmd: str, args: Sequence[str]) -> GitResult:
        """``git log|show|blame`` with allowlisted options, commits and PR paths.

        Every revision must name a commit, so file content is reachable only as
        ``show <commit>:<path>`` or ``blame``; those obey the same skip rules and per-file
        allowance as ``repo_read``. Paths are checked with ``safe_path`` but passed to git
        as written, because git looks them up in commits, where symlinks aren't followed.

        Raises:
            NoReviewSessionError, UnsafeGitArgumentError (NotACommitError), UnsafePathError,
            AutoSkippedFileError, AllowanceExhaustedError, RepoGitError.
        """
        session, root = self._session(session_id)
        plan = plan_git_command(cmd, args)
        for path in (*plan.paths, *(blob_path for _, blob_path in plan.blobs)):
            safe_path(root, path)
        revisions = [await self._commit(root, revision) for revision in plan.revisions]
        blobs = [(await self._commit(root, rev), path) for rev, path in plan.blobs]
        # The one file whose content this command prints, if any.
        content_of = blobs[0][1] if blobs else plan.paths[0] if cmd == "blame" else None
        key = None
        granted = GIT_MAX_LINES
        if content_of is not None:
            self._refuse_skipped(content_of, False)
            self._refuse_skipped(_relative(root, safe_path(root, content_of)), False)
            key = _allowance_key(root, content_of)
            granted = await self._reserve(session, key, content_of, GIT_MAX_LINES)
        if cmd == "blame":
            revision = revisions[0] if revisions else "HEAD"
            argv = ["blame", "--no-ext-diff", "--no-textconv", *plan.options]
            argv += ["--end-of-options", revision, plan.paths[0]]
        else:
            argv = [cmd, "--no-ext-diff", "--no-textconv", "--no-color", *plan.options]
            argv += ["--end-of-options", *revisions, *(f"{r}:{p}" for r, p in blobs)]
            argv += ["--", *plan.paths]
        lines: list[str] = []
        try:
            output = await self._run_git(root, argv, output_limit=GIT_MAX_BYTES)
            data = output.data
            if output.truncated:
                data = data[: data.rfind(b"\n") + 1]  # whole lines only
            if content_of is not None:
                self._refuse_skipped(content_of, _is_binary(data))
            every_line = data.decode(errors="replace").splitlines(keepends=True)
            lines = every_line[: min(granted, GIT_MAX_LINES)]
        finally:
            if key is not None:
                self._allowance.refund(session, key, granted - len(lines))
        truncated = output.truncated or len(every_line) > len(lines)
        cap = (
            f"repo_git returns at most {GIT_MAX_LINES} lines and {GIT_MAX_BYTES // 1024} KB "
            "per call, -p diffs included"
        )
        if key is not None:
            cap += (
                f"; {content_of} content counts against its {FILE_LINES_PER_SESSION}-line "
                f"allowance ({self._allowance.remaining(session, key)} lines remain)"
            )
        return {"cmd": cmd, "output": "".join(lines), "truncated": truncated, "cap": cap}

    def _session(self, session_id: str | None) -> tuple[str, Path]:
        if session_id is None:
            raise NoReviewSessionError(
                "the repo tools work only inside a review session started by Jesinia"
            )
        tree = self._sessions_to_worktree(session_id)
        if tree is None:
            raise NoReviewSessionError(f"session {session_id!r} is not reviewing a PR")
        if not tree.is_dir():
            raise NoReviewSessionError("the worktree for this review session no longer exists")
        return session_id, tree.resolve()

    async def _reserve(self, session: str, key: str, rel: str, lines: int) -> int:
        """Reserve up to ``lines`` of a file's allowance before reading anything.

        Reserving up front, then refunding what wasn't returned, is what stops parallel
        tool calls from each seeing the same remaining allowance. The per-session lock
        keeps the check and the reservation one step even if this ever needs to await.

        Raises:
            AllowanceExhaustedError: nothing of the file's allowance is left.
        """
        async with self._locks.setdefault(session, asyncio.Lock()):
            granted = self._allowance.take(session, key, lines)
        if granted == 0:
            raise AllowanceExhaustedError(
                f"this review session has read its {FILE_LINES_PER_SESSION} lines per file "
                f"of {rel}; work from what you have read"
            )
        return granted

    async def _commit(self, root: Path, revision: str) -> str:
        """The commit ``revision`` names, as a full id.

        Raises:
            NotACommitError: a blob, a tree, or nothing.
        """
        output = await self._run_git(
            root,
            ["rev-parse", "--verify", "-q", "--end-of-options", f"{revision}^{{commit}}"],
            output_limit=4096,
            accept_exit_codes=frozenset({0, 1}),  # 1: not a commit
        )
        commit = output.data.decode(errors="replace").strip()
        if not commit:
            raise NotACommitError(
                f"{revision!r} does not name a commit. Read file content with repo_read, "
                "or with show <commit>:<path>"
            )
        return commit

    @staticmethod
    def _refuse_skipped(rel: str, is_binary: bool) -> None:
        reason = auto_skip_reason(rel, is_binary)
        if reason is not None:
            raise AutoSkippedFileError(f"{rel} is skipped by the coverage rules ({reason})")

    async def _run_git(
        self,
        root: Path,
        args: Sequence[str],
        *,
        output_limit: int,
        glob_pathspecs: bool = False,
        accept_exit_codes: frozenset[int] = frozenset({0}),
    ) -> CappedOutput:
        try:
            return await self._git.read_in_tree(
                root,
                args,
                output_limit=output_limit,
                glob_pathspecs=glob_pathspecs,
                accept_exit_codes=accept_exit_codes,
            )
        except WorkspaceError as exc:
            detail = exc.stderr.strip() if isinstance(exc, GitCommandError) else str(exc)
            raise RepoGitError(f"git {args[0]} failed: {detail}") from exc


def _stays(root: Path, rel: str) -> bool:
    try:
        safe_path(root, rel)
    except UnsafePathError:
        return False
    return True
