# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo tools: how review agents read a PR's code, and only that PR's code.

Each call is resolved to the worktree bound to the caller's review session. Paths must
resolve inside it (symlinks included); files the coverage rules skip are refused; every
result is capped and says what its caps are. Searching and history go through git
(``TreeGit``), which reads git objects rather than following symlinks on disk.
"""

from __future__ import annotations

import asyncio
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
    OutsidePrHistoryError,
    ReadLog,
    RegexDialect,
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
from jesinia.apps.review.domain.skip import (
    auto_skip_reason,
    directory_skip_reason,
    skip_pathspecs,
)
from jesinia.apps.review.domain.workspace import GitCommandError, WorkspaceError

# Seconds a search (grep, log --author=) may run: regexes are the one input that can make
# git work hard. Other commands keep the workspace's default.
SEARCH_TIMEOUT = 20.0

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
    escaped_paths: list[str]
    skipped_files: list[str]
    cap: str


class LsEntry(TypedDict):
    name: str
    kind: str  # "file", "dir" or "symlink"
    size: NotRequired[int]  # files
    skip: NotRequired[str | None]  # files and dirs: the auto-skip reason
    inside: NotRequired[bool]  # symlinks: whether the target stays inside the PR
    escaped: NotRequired[bool]  # the name isn't UTF-8; shown with \\xNN escapes


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
            # Counted in UTF-8 after decoding: undecodable bytes grow into three-byte
            # replacement characters.
            line = raw.decode(errors="replace")
            encoded = len(line.encode())
            if size + encoded > READ_MAX_BYTES:
                if not lines:
                    cut = line.encode()[:READ_MAX_BYTES]
                    lines.append(cut.decode(errors="ignore"))
                return lines, True, True
            lines.append(line)
            size += encoded
    return lines, False, False


def _display_bytes(raw: bytes) -> tuple[str, bool]:
    """A path from git as JSON-safe text, and whether it had to be escaped: bytes that
    aren't UTF-8 are shown as ``\\xNN`` rather than crashing the encoder later."""
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", "backslashreplace"), True


def _display_name(name: str) -> tuple[str, bool]:
    """``_display_bytes`` for a name from the filesystem (undecodable bytes arrive as
    surrogate escapes)."""
    return _display_bytes(name.encode("utf-8", "surrogateescape"))


def _mark_escaped(entry: LsEntry, escaped: bool) -> LsEntry:
    if escaped:
        entry["escaped"] = True
    return entry


def _within_bytes(lines: list[str], limit: int) -> list[str]:
    """The leading lines whose UTF-8 encoding fits in ``limit`` bytes. Output is capped
    after decoding, because undecodable bytes grow threefold as replacement characters."""
    size = 0
    for count, line in enumerate(lines):
        size += len(line.encode())
        if size > limit:
            return lines[:count]
    return lines


def _allowance_key(root: Path, rel: str) -> str:
    """The allowance key of a worktree path: the file's identity on disk, so case, Unicode
    normalization, symlink and hardlink spellings of one file share one allowance. A path
    with no file on disk (deleted in the PR) is keyed by its folded spelling."""
    try:
        stat = (root / rel).stat()
    except OSError:
        return "path:" + unicodedata.normalize("NFC", rel).casefold()
    return f"inode:{stat.st_dev}:{stat.st_ino}"


def _parse_grep(data: bytes) -> tuple[list[tuple[bytes, int, bytes]], bool]:
    """``git grep -n -z`` records as ``(path, line, text)``, and whether the output ended
    inside a record.

    Records are ``<path>\\0<line>\\0<text>\\n``. A path may contain a newline but never a
    NUL; matched text never contains a newline but may contain NULs (git only sniffs the
    first 8000 bytes for binary). So the text runs from after the line number to the first
    newline, rejoining any NUL-split pieces, and the next path starts right after it. An
    unfinished final record (output cut at the byte cap) is reported, never dropped quietly.

    Raises:
        RepoGitError: a line number that isn't a number.
    """
    fields = data.split(b"\0")
    records: list[tuple[bytes, int, bytes]] = []
    path = fields[0]
    index = 1
    while index < len(fields):
        line = fields[index]
        pieces: list[bytes] = []
        newline = b""
        while not newline:
            index += 1
            if index == len(fields):
                return records, True  # the output ended inside this record
            text, newline, following = fields[index].partition(b"\n")
            pieces.append(text)
        if not line.isdigit():
            raise RepoGitError("could not parse git grep output; narrow the search with a glob")
        records.append((path, int(line), b"\0".join(pieces)))
        path = following
        index += 1
    return records, bool(path)


class RepoTools:
    """``repo_read``, ``repo_grep``, ``repo_ls`` and ``repo_git`` over review worktrees.

    Args:
        sessions_to_worktree: the worktree bound to a review session, or ``None``.
        git: read-only git inside a worktree.
        allowance: per-session, per-file line allowance shared by every tool.
        tree_key: the canonical ``(repo, sha)`` of a worktree path (the workspace's), so
            results can name what they were read from.
        reads: where the lines ``repo_read`` and ``repo_grep`` return from the head are
            recorded per session (the registry's), so the correctness pass's claims can be
            checked against them. ``repo_git`` output isn't recorded: its line numbers
            belong to other commits.
    """

    def __init__(
        self,
        sessions_to_worktree: Callable[[str], Path | None],
        git: TreeGit,
        allowance: FileLineAllowance,
        *,
        git_slots: Callable[[str], asyncio.Semaphore] | None = None,
        tree_key: Callable[[Path], tuple[str, str] | None] | None = None,
        reads: ReadLog | None = None,
    ) -> None:
        self._sessions_to_worktree = sessions_to_worktree
        self._reads = reads
        self._git = git
        self._allowance = allowance
        self._tree_key = tree_key
        own_slots: dict[str, asyncio.Semaphore] = {}
        self._git_slots = git_slots or (
            lambda session: own_slots.setdefault(session, asyncio.Semaphore(1))
        )

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
            hint = (
                "; paths shown with \\xNN escapes aren't valid UTF-8 and can't be read with "
                "repo_read (try repo_grep, or repo_git show)"
                if "\\x" in path
                else ""
            )
            raise RepoPathNotFoundError(f"no file at {path!r}{hint}")
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
        granted = self._reserve(session, key, rel, wanted)
        lines: list[str] = []
        try:
            lines, has_more, byte_capped = await asyncio.to_thread(
                _read_lines, target, first, granted
            )
        finally:
            self._allowance.refund(session, key, granted - len(lines))
        if self._reads is not None:
            self._reads.record(session, rel, first, first + len(lines) - 1)
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
        dialect = await self._dialect()
        plain = is_plain_text(check_pattern(pattern, dialect))
        args += ["--no-textconv", "-F" if plain else "-E"]
        args += ["-e", pattern if plain else posix_ere(pattern, dialect)]
        # Skipped files are excluded by git itself, so a vendored flood of matches can't
        # use up the raw output budget before the PR's own files are reached.
        args += ["--", *([] if glob is None else [check_glob(glob)]), *skip_pathspecs()]
        output = await self._run_git(
            session,
            root,
            args,
            timeout=SEARCH_TIMEOUT,
            output_limit=GREP_RAW_BYTES,
            glob_pathspecs=True,  # the :(glob) and :(exclude,glob,icase) pathspecs
            accept_exit_codes=frozenset({0, 1}),  # 1: nothing matched
        )
        matches: list[GrepMatch] = []
        skipped: set[str] = set()
        keys: dict[str, str] = {}
        withheld = 0
        size = 0
        records, unfinished = _parse_grep(output.data)
        # Only output already cut at the byte cap may end mid-record; either way it is
        # reported as truncated, so no match is ever missing silently.
        truncated = output.truncated or unfinished
        escaped_paths: set[str] = set()
        for raw_path, line, raw_text in records:
            rel, escaped = _display_bytes(raw_path)
            if escaped:
                escaped_paths.add(rel)
            # repo_read refuses control characters in paths, so they are skipped here too.
            if auto_skip_reason(rel, False) is not None or any(ord(c) < 32 for c in rel):
                skipped.add(rel)
                continue
            text = raw_text.decode(errors="replace")
            # The cap is on UTF-8 bytes: an emoji is one character but four bytes, and
            # every undecodable byte becomes a three-byte replacement character.
            shown = text[:GREP_MAX_LINE_CHARS]
            entry_size = len(rel.encode()) + len(shown.encode()) + 16
            if len(matches) >= GREP_MAX_MATCHES or size + entry_size > GREP_MAX_BYTES:
                truncated = True
                continue
            key = keys.setdefault(rel, _allowance_key(root, rel))
            if self._allowance.take(session, key, 1) == 0:
                withheld += 1
                continue
            size += entry_size
            if self._reads is not None:
                self._reads.record(session, rel, line, line)
            matches.append(
                {
                    "path": rel,
                    "line": line,
                    "text": shown,
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
            "escaped_paths": sorted(escaped_paths),
            "cap": (
                f"repo_grep returns at most {GREP_MAX_MATCHES} matches and "
                f"{GREP_MAX_BYTES // 1024} KB per call, each line cut to "
                f"{GREP_MAX_LINE_CHARS} characters; matched lines count against the "
                f"{FILE_LINES_PER_SESSION}-line per-file allowance, and binary, lockfile, "
                "generated and vendored files are not searched. Paths that aren't valid "
                "UTF-8 are shown with \\xNN escapes (listed in escaped_paths)."
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
        # A PR directory can hold 100k entries: list and stat them off the event loop.
        return await asyncio.to_thread(_list_directory, root, target, LS_MAX_ENTRIES)

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
        plan = plan_git_command(cmd, args, dialect=await self._dialect())
        for path in (*plan.paths, *(blob_path for _, blob_path in plan.blobs)):
            safe_path(root, path)
        revisions = [await self._commit(session, root, revision) for revision in plan.revisions]
        blobs = [(await self._commit(session, root, rev), path) for rev, path in plan.blobs]
        # The one file whose content this command prints, if any.
        content_of = blobs[0][1] if blobs else plan.paths[0] if cmd == "blame" else None
        key = None
        granted = GIT_MAX_LINES
        if content_of is not None:
            self._refuse_skipped(content_of, False)
            self._refuse_skipped(_relative(root, safe_path(root, content_of)), False)
            key = _allowance_key(root, content_of)
            granted = self._reserve(session, key, content_of, GIT_MAX_LINES)
        if cmd == "blame":
            # Run without the worktree (see TreeGit), so HEAD is resolved here first.
            revision = revisions[0] if revisions else await self._commit(session, root, "HEAD")
            # No --end-of-options: git 2.43's blame misparses `--end-of-options <rev> --
            # <path>` ("bad revision ''"). Nothing here can pass for an option anyway: the
            # revision is a full commit id from rev-parse and the path follows `--`.
            argv = ["blame", "--no-ext-diff", "--no-textconv", *plan.options]
            argv += [revision, "--", plan.paths[0]]
        else:
            argv = [cmd, "--no-ext-diff", "--no-textconv", "--no-color", *plan.options]
            argv += ["--end-of-options", *revisions, *(f"{r}:{p}" for r, p in blobs)]
            argv += ["--", *plan.paths]
        lines: list[str] = []
        try:
            searches = any(option.startswith("--author=") for option in plan.options)
            output = await self._run_git(
                session,
                root,
                argv,
                output_limit=GIT_MAX_BYTES,
                without_worktree=cmd == "blame",
                timeout=SEARCH_TIMEOUT if searches else None,
            )
            data = output.data
            if output.truncated:
                data = data[: data.rfind(b"\n") + 1]  # whole lines only
            if content_of is not None:
                self._refuse_skipped(content_of, _is_binary(data))
            every_line = data.decode(errors="replace").splitlines(keepends=True)
            lines = _within_bytes(every_line[: min(granted, GIT_MAX_LINES)], GIT_MAX_BYTES)
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

    def origin(self, session_id: str | None) -> str:
        """``<repo>@<sha>`` of the worktree ``session_id`` reads (``unknown@<tree name>``
        when the workspace can't name it), for labelling results as untrusted data.

        Raises:
            NoReviewSessionError: the session isn't bound to a worktree.
        """
        _, root = self._session(session_id)
        key = None if self._tree_key is None else self._tree_key(root)
        return f"unknown@{root.name}" if key is None else f"{key[0]}@{key[1]}"

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

    async def _dialect(self) -> RegexDialect:
        try:
            return await self._git.regex_dialect()
        except WorkspaceError as exc:
            raise RepoGitError(f"could not probe git's regex support: {exc}") from exc

    def _reserve(self, session: str, key: str, rel: str, lines: int) -> int:
        """Reserve up to ``lines`` of a file's allowance before reading anything.

        Reserving up front, then refunding what wasn't returned, is what stops parallel
        tool calls from each seeing the same remaining allowance. There is no await
        between the check and the take; on one event loop, that is the atomicity.

        Raises:
            AllowanceExhaustedError: nothing of the file's allowance is left.
        """
        granted = self._allowance.take(session, key, lines)
        if granted == 0:
            raise AllowanceExhaustedError(
                f"this review session has read its {FILE_LINES_PER_SESSION} lines per file "
                f"of {rel}; work from what you have read"
            )
        return granted

    async def _commit(self, session: str, root: Path, revision: str) -> str:
        """The commit ``revision`` names, as a full id, if it is part of the PR's history.

        The mirror holds every fetched branch and every other PR's head; agents read only
        the PR under review, which includes the base history it was built on (R17).

        Raises:
            NotACommitError: a blob, a tree, or nothing.
            OutsidePrHistoryError: a commit that isn't an ancestor of the worktree's HEAD.
        """
        output = await self._run_git(
            session,
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
        ancestry = await self._run_git(
            session,
            root,
            ["merge-base", "--is-ancestor", "--end-of-options", commit, "HEAD"],
            output_limit=4096,
            accept_exit_codes=frozenset({0, 1}),  # 1: not an ancestor
        )
        if ancestry.exit_code != 0:
            raise OutsidePrHistoryError(
                f"{revision!r} is not part of this PR's history (an ancestor of its head)"
            )
        return commit

    @staticmethod
    def _refuse_skipped(rel: str, is_binary: bool) -> None:
        reason = auto_skip_reason(rel, is_binary)
        if reason is not None:
            raise AutoSkippedFileError(f"{rel} is skipped by the coverage rules ({reason})")

    async def _run_git(
        self,
        session: str,
        root: Path,
        args: Sequence[str],
        *,
        output_limit: int,
        glob_pathspecs: bool = False,
        accept_exit_codes: frozenset[int] = frozenset({0}),
        without_worktree: bool = False,
        timeout: float | None = None,
    ) -> CappedOutput:
        """Run git for ``session``, one process at a time per session: parallel tool calls
        queue here instead of each taking cores."""
        try:
            async with self._git_slots(session):
                return await self._git.read_in_tree(
                    root,
                    args,
                    output_limit=output_limit,
                    glob_pathspecs=glob_pathspecs,
                    accept_exit_codes=accept_exit_codes,
                    without_worktree=without_worktree,
                    timeout=timeout,
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


def _list_directory(root: Path, target: Path, limit: int) -> LsResult:
    entries: list[LsEntry] = []
    children = sorted(child for child in target.iterdir() if child.name.lower() != ".git")
    for child in children[:limit]:
        rel = _relative(root, child)
        name, escaped = _display_name(child.name)
        entry: LsEntry
        if child.is_symlink():
            entry = {"name": name, "kind": "symlink", "inside": _stays(root, rel)}
        elif child.is_dir():
            entry = {"name": name, "kind": "dir", "skip": directory_skip_reason(rel)}
        else:
            entry = {
                "name": name,
                "kind": "file",
                "size": child.lstat().st_size,
                "skip": auto_skip_reason(rel, False),
            }
        entries.append(_mark_escaped(entry, escaped))
    return {
        "path": _display_name(_relative(root, target))[0],
        "entries": entries,
        "truncated": len(children) > limit,
        "cap": f"repo_ls lists at most {limit} entries per directory",
    }
