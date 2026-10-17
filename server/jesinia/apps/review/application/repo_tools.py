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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NotRequired, TypedDict

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
    RepoGitError,
    RepoPathNotFoundError,
    UnsafePathError,
    check_glob,
    check_pattern,
    normalize_relative_path,
    plan_git_command,
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


def _read_lines(path: Path, first: int, count: int) -> tuple[list[str], bool, bool]:
    """Lines ``first``.. (at most ``count``) of ``path``, whether more follow, and whether
    the byte cap stopped the read. A single line longer than the cap is cut to it."""
    lines: list[str] = []
    size = 0
    byte_capped = False
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, start=1):
            if number < first:
                continue
            if len(lines) == count:
                return lines, True, byte_capped
            if size + len(raw) > READ_MAX_BYTES:
                if not lines:
                    lines.append(raw[:READ_MAX_BYTES].decode(errors="replace"))
                return lines, True, True
            lines.append(raw.decode(errors="replace"))
            size += len(raw)
    return lines, False, byte_capped


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
        remaining = self._remaining(session, rel)
        wanted = last - first + 1
        budget = min(wanted, READ_LINES_PER_CALL, remaining)
        lines, has_more, byte_capped = await asyncio.to_thread(_read_lines, target, first, budget)
        self._allowance.take(session, rel, len(lines))
        truncated = has_more and len(lines) < wanted
        cap = (
            f"repo_read returns at most {READ_LINES_PER_CALL} lines per call (and at most "
            f"{READ_MAX_BYTES // 1024} KB), and {FILE_LINES_PER_SESSION} lines per file per "
            "review session; "
            f"{self._allowance.remaining(session, rel)} lines of {rel} remain in this "
            "session's allowance."
        )
        if truncated and byte_capped:
            cap += f" This read stopped at {READ_MAX_BYTES} bytes."
        elif truncated and budget == remaining < min(wanted, READ_LINES_PER_CALL):
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
            "allowance_remaining": self._allowance.remaining(session, rel),
            "cap": cap,
        }

    async def grep(
        self, session_id: str | None, pattern: str, glob: str | None = None
    ) -> GrepResult:
        """Search the PR's tracked files with an extended regex (``git grep -E``).

        Binary and auto-skipped files are left out; matched lines count against each file's
        allowance.

        Raises:
            NoReviewSessionError, InvalidSearchError, RepoGitError.
        """
        session, root = self._session(session_id)
        # --cached: search the checked-out commit's blobs, never files (or symlink targets)
        # on disk; --no-column: a user's grep.column=true would change the record format.
        args = ["grep", "--cached", "-I", "-n", "-z", "--no-column", "--no-color"]
        args += ["--no-textconv", "-E"]
        args += ["-e", check_pattern(pattern)]
        if glob is not None:
            args += ["--", check_glob(glob)]
        output = await self._run_git(
            root,
            args,
            output_limit=GREP_RAW_BYTES,
            glob_pathspecs=glob is not None,
            accept_exit_codes=frozenset({0, 1}),  # 1: nothing matched
        )
        records = output.data.split(b"\n")
        if output.truncated:
            records.pop()  # the last record was cut mid-line
        matches: list[GrepMatch] = []
        skipped: set[str] = set()
        withheld = 0
        size = 0
        truncated = output.truncated
        for record in filter(None, records):
            raw_path, _, rest = record.partition(b"\0")
            raw_line, _, raw_text = rest.partition(b"\0")
            rel = os.fsdecode(raw_path)
            if auto_skip_reason(rel, False) is not None:
                skipped.add(rel)
                continue
            text = raw_text.decode(errors="replace")
            entry_size = len(raw_path) + min(len(text), GREP_MAX_LINE_CHARS) + 16
            if len(matches) >= GREP_MAX_MATCHES or size + entry_size > GREP_MAX_BYTES:
                truncated = True
                continue
            if self._allowance.take(session, rel, 1) == 0:
                withheld += 1
                continue
            size += entry_size
            matches.append(
                {
                    "path": rel,
                    "line": int(raw_line),
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
        """``git log|show|blame`` with allowlisted options, revisions and PR paths.

        ``show <rev>:<path>`` and ``blame`` print a file's content, so they obey the same
        skip rules and per-file allowance as ``repo_read``.

        Raises:
            NoReviewSessionError, UnsafeGitArgumentError, UnsafePathError,
            AutoSkippedFileError, AllowanceExhaustedError, RepoGitError.
        """
        session, root = self._session(session_id)
        plan = plan_git_command(cmd, args)
        paths = [_relative(root, safe_path(root, p)) for p in plan.paths]
        blobs = [(rev, _relative(root, safe_path(root, p))) for rev, p in plan.blobs]
        # The one file whose content this command prints, if any.
        content_of = blobs[0][1] if blobs else paths[0] if cmd == "blame" else None
        line_cap = GIT_MAX_LINES
        if content_of is not None:
            self._refuse_skipped(content_of, False)
            line_cap = min(line_cap, self._remaining(session, content_of))
        if cmd == "blame":
            revision = plan.revisions[0] if plan.revisions else "HEAD"
            argv = ["blame", "--no-ext-diff", "--no-textconv", *plan.options]
            argv += ["--end-of-options", revision, paths[0]]
        else:
            argv = [cmd, "--no-ext-diff", "--no-textconv", "--no-color", *plan.options]
            argv += ["--end-of-options", *plan.revisions, *(f"{r}:{p}" for r, p in blobs)]
            argv += ["--", *paths]
        output = await self._run_git(root, argv, output_limit=GIT_MAX_BYTES)
        data = output.data
        if output.truncated:
            data = data[: data.rfind(b"\n") + 1]  # whole lines only
        if content_of is not None:
            self._refuse_skipped(content_of, _is_binary(data))
        lines = data.decode(errors="replace").splitlines(keepends=True)
        truncated = output.truncated or len(lines) > line_cap
        lines = lines[:line_cap]
        cap = (
            f"repo_git returns at most {GIT_MAX_LINES} lines and {GIT_MAX_BYTES // 1024} KB "
            "per call"
        )
        if content_of is not None:
            self._allowance.take(session, content_of, len(lines))
            cap += (
                f"; {content_of} content counts against its {FILE_LINES_PER_SESSION}-line "
                f"allowance ({self._allowance.remaining(session, content_of)} lines remain)"
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

    def _remaining(self, session: str, rel: str) -> int:
        remaining = self._allowance.remaining(session, rel)
        if remaining <= 0:
            raise AllowanceExhaustedError(
                f"this review session has read its {FILE_LINES_PER_SESSION} lines per file "
                f"of {rel}; work from what you have read"
            )
        return remaining

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
