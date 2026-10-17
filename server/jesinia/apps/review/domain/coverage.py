# SPDX-License-Identifier: AGPL-3.0-or-later
"""Walkthrough coverage (spec §3.1): how much of a PR's change a walkthrough, or the
correctness pass, actually covers. Pure: diff text and agent output in, numbers out.

Jesinia computes it, never the agent: covered changed lines over all changed lines, plus a
file count, from the hunks the steps cite against the full diff. Nothing is left out
silently. Every file or hunk not covered is listed with its reason: Jesinia's own
(``lockfile``, ``vendored``, ``generated``, ``binary``, ``rename_only``, ``metadata_only``),
the agent's (``generated``, ``lockfile``, ``vendored``, ``rename_only``, ``format_only``,
``judged_mechanical``), or ``omitted`` when nobody gave one. A step that cites a hunk the
diff doesn't have is dropped whole, so the lines it claimed count as not covered.

The cost is bounded by what a diff changes, not by its bytes: changed lines are kept as
runs (never one object per line), a diff over ``MAX_CHANGED_LINES`` changed lines or
``MAX_FILES`` files is refused while it is read, coverage is interval arithmetic, and the
"not walked" list keeps at most ``NOT_WALKED_LIMIT`` entries beside its full count.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Final, Literal

from jesinia.apps.review.domain.skip import auto_skip_reason
from jesinia.apps.review.domain.workspace import DiffTooLargeError

SkipReason = Literal[
    "generated",
    "lockfile",
    "vendored",
    "binary",
    "rename_only",
    "format_only",
    "judged_mechanical",
    "metadata_only",
    "omitted",
]
# The reasons a walkthrough agent may give (its schema's enum).
AGENT_SKIP_REASONS: Final = frozenset(
    {"generated", "lockfile", "vendored", "rename_only", "format_only", "judged_mechanical"}
)
FileStatus = Literal["added", "deleted", "modified", "renamed"]
LineKind = Literal["+", "-"]
# A diff that couldn't be read in full never shows as fully covered (it can't be known).
INCOMPLETE_CEILING: Final = 99.0
# Past these a diff isn't read (it is incomplete instead): bounds on time and memory that a
# PR's size can't push, whatever its bytes (ruling R41).
MAX_CHANGED_LINES: Final = 200_000
MAX_FILES: Final = 20_000
# Entries of "not walked" kept; ``not_walked_total`` counts them all.
NOT_WALKED_LIMIT: Final = 500
# Characters of the hunk index an agent is given; one file's listing may take at most
# 1/SUMMARY_SHARE of it before it is summarised in one line.
HUNK_INDEX_CHARS: Final = 60_000
SUMMARY_SHARE: Final = 8

_DIFF_HEADER = "diff --git "
# Ten digits: git's line numbers fit, and int() never sees an absurd string.
_HUNK_HEADER = re.compile(r"@@ -(\d{1,10})(?:,(\d{1,10}))? \+(\d{1,10})(?:,(\d{1,10}))? @@")
_ESCAPES: Final = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


class CoverageError(ValueError):
    """Base for coverage inputs Jesinia can't work with."""


class DiffParseError(CoverageError):
    """The diff text isn't git's unified diff format."""


@dataclass(frozen=True)
class Run:
    """Consecutive changed lines of one kind: added lines ``start``..``end`` of the new file
    (``+``), or removed lines ``start``..``end`` of the old file (``-``). ``anchor`` is where
    the run sits in the new file: its first line for added lines, or the new line the
    removed ones were in front of."""

    kind: LineKind
    start: int
    end: int
    anchor: int

    @property
    def count(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    runs: tuple[Run, ...]

    @property
    def changed(self) -> int:
        return sum(run.count for run in self.runs)

    @property
    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"


@dataclass(frozen=True)
class FileDiff:
    """One file's diff; ``path`` is the new path (the old one for a deleted file)."""

    path: str
    old_path: str
    status: FileStatus
    is_binary: bool
    hunks: tuple[Hunk, ...]

    @property
    def changed_lines(self) -> int:
        return sum(hunk.changed for hunk in self.hunks)


@dataclass(frozen=True)
class ParsedDiff:
    """A PR's whole diff. ``complete`` is false when it couldn't be read in full (too large);
    ``expected_lines`` and ``expected_files`` (GitHub's counts) then size what is missing."""

    files: tuple[FileDiff, ...]
    complete: bool = True
    incomplete_reason: str | None = None
    expected_lines: int = 0
    expected_files: int = 0

    @property
    def changed_lines(self) -> int:
        return sum(f.changed_lines for f in self.files)

    @cached_property
    def _hunks_by_path(self) -> dict[str, list[tuple[int, int]]]:
        """Each path's hunks as ``(file position, hunk index in that entry)``, numbered on
        across the entries of one path: git lists a type change (a file becoming a symlink)
        as two entries with the same path, and both count."""
        slots: dict[str, list[tuple[int, int]]] = {}
        for position, diff_file in enumerate(self.files):
            entry = slots.setdefault(diff_file.path, [])
            entry.extend((position, h) for h in range(len(diff_file.hunks)))
        return slots

    @cached_property
    def _offsets(self) -> list[int]:
        """Per entry, how many hunks earlier entries of the same path have."""
        seen: dict[str, int] = {}
        offsets: list[int] = []
        for diff_file in self.files:
            offsets.append(seen.get(diff_file.path, 0))
            seen[diff_file.path] = offsets[-1] + len(diff_file.hunks)
        return offsets

    def has_path(self, path: str) -> bool:
        return path in self._hunks_by_path

    def locate(self, path: str, hunk: int) -> tuple[int, int] | None:
        """The entry and its own hunk index for hunk ``hunk`` of ``path`` as agents cite it
        (numbered across every entry of the path); ``None`` if there is no such hunk."""
        slots = self._hunks_by_path.get(path, [])
        return slots[hunk] if 0 <= hunk < len(slots) else None

    def cited_index(self, position: int, hunk: int) -> int:
        """How agents cite hunk ``hunk`` of the entry at ``position``."""
        return self._offsets[position] + hunk


@dataclass(frozen=True)
class HunkRef:
    """A hunk as an agent cites it: the file's path and its 0-based index in that file."""

    path: str
    hunk: int


@dataclass(frozen=True)
class Step:
    """A walkthrough step: the hunks it walks through."""

    hunks: tuple[HunkRef, ...]
    title: str = ""


@dataclass(frozen=True)
class Skip:
    """A file (``hunk`` ``None``) or hunk left out, and why."""

    path: str
    hunk: int | None
    reason: str


@dataclass(frozen=True)
class LineRange:
    """Lines ``start`` to ``end`` (inclusive) of ``path`` that the correctness pass read."""

    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Coverage:
    """What a walkthrough (or the correctness pass) covers.

    ``pct`` is covered changed lines over all changed lines, lockfiles and binaries
    included: the honest headline. ``reviewable_pct`` leaves out the lines of files
    Jesinia itself skips (lockfiles, vendored, generated, binaries) and of deleted files
    (nothing of them is left at the head to read); the evidence rule reads it. Neither
    reaches 100 for an incomplete diff. ``not_walked`` keeps the first
    ``NOT_WALKED_LIMIT`` entries; ``not_walked_total`` counts them all.
    """

    pct: float
    files_covered: int
    files_total: int
    not_walked: list[Skip]
    lines_covered: int = 0
    lines_total: int = 0
    lines_auto_skipped: int = 0
    reviewable_pct: float = 100.0
    dropped_steps: int = 0
    complete: bool = True
    incomplete_reason: str | None = None
    not_walked_total: int = 0

    @property
    def nothing_to_walk(self) -> bool:
        """The diff, read in full, changes no line (only renames, binaries, modes): its
        100% means there was nothing to cover, which the UI says instead (M3)."""
        return self.complete and self.lines_total == 0

    def as_json(self) -> dict[str, object]:
        return {
            "pct": self.pct,
            "files_covered": self.files_covered,
            "files_total": self.files_total,
            "lines_covered": self.lines_covered,
            "lines_total": self.lines_total,
            "lines_auto_skipped": self.lines_auto_skipped,
            "reviewable_pct": self.reviewable_pct,
            "dropped_steps": self.dropped_steps,
            "complete": self.complete,
            "incomplete_reason": self.incomplete_reason,
            "not_walked": [
                {"path": skip.path, "hunk": skip.hunk, "reason": skip.reason}
                for skip in self.not_walked
            ],
            "not_walked_total": self.not_walked_total,
            "nothing_to_walk": self.nothing_to_walk,
        }


# --- parsing ---------------------------------------------------------------------------------


def _unquote(text: str) -> tuple[str, str]:
    """Read one path from the start of ``text``: git's C-style quoted form (``"a/caf\\303"``)
    or a bare one up to the end. Returns the path and what follows it."""
    if not text.startswith('"'):
        return text, ""
    out = bytearray()
    index = 1
    while index < len(text):
        char = text[index]
        if char == '"':
            return out.decode("utf-8", errors="replace"), text[index + 1 :]
        if char != "\\":
            out += char.encode()
            index += 1
            continue
        escape = text[index + 1 : index + 2]
        if escape in _ESCAPES:
            out.append(_ESCAPES[escape])
            index += 2
        elif re.fullmatch(r"[0-7]{3}", text[index + 1 : index + 4]):
            out.append(int(text[index + 1 : index + 4], 8))
            index += 4
        else:
            raise DiffParseError(f"bad escape in a quoted path: {text[:80]!r}")
    raise DiffParseError(f"unterminated quoted path: {text[:80]!r}")


def _strip_side(path: str, side: str) -> str:
    return path[2:] if path.startswith(f"{side}/") else path


def _header_paths(rest: str) -> tuple[str, str]:
    """The old and new paths of a ``diff --git a/<old> b/<new>`` line. Unquoted names with
    spaces are ambiguous there, so a name equal on both sides is found by symmetry; a
    rename's real names come from its ``rename from``/``rename to`` lines."""
    if rest.startswith('"'):
        old, after = _unquote(rest)
        new, _ = _unquote(after.lstrip(" "))
        return _strip_side(old, "a"), _strip_side(new, "b")
    half = (len(rest) - 5) // 2
    if half > 0 and rest == f"a/{rest[2 : 2 + half]} b/{rest[2 : 2 + half]}":
        name = rest[2 : 2 + half]
        return name, name
    old, separator, new = rest.partition(" b/")
    if not separator:
        old, separator, new = rest.partition(' "b/')
        if not separator:
            raise DiffParseError(f"unreadable diff header: {rest[:80]!r}")
        new, _ = _unquote(f'"b/{new}')
    return _strip_side(old, "a"), _strip_side(new, "b")


class _Lines:
    """git's output one line at a time, without splitting it all up front."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._at = 0

    def peek(self) -> str | None:
        if self._at >= len(self._text):
            return None
        end = self._text.find("\n", self._at)
        return self._text[self._at : len(self._text) if end < 0 else end]

    def take(self) -> str | None:
        line = self.peek()
        if line is not None:
            self._at += len(line) + 1
        return line


@dataclass
class _FileBuilder:
    old_path: str
    path: str
    status: FileStatus = "modified"
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    def header(self, line: str) -> None:
        if line.startswith("new file mode"):
            self.status = "added"
        elif line.startswith("deleted file mode"):
            self.status = "deleted"
        elif line.startswith("rename from "):
            self.old_path, _ = _unquote(line.removeprefix("rename from "))
            self.status = "renamed"
        elif line.startswith("rename to "):
            self.path, _ = _unquote(line.removeprefix("rename to "))
            self.status = "renamed"
        elif line.startswith("Binary files ") or line == "GIT binary patch":
            self.is_binary = True

    def build(self) -> FileDiff:
        path = self.old_path if self.status == "deleted" else self.path
        return FileDiff(path, self.old_path, self.status, self.is_binary, tuple(self.hunks))


class _Budget:
    """Changed lines left before the diff counts as too large."""

    def __init__(self) -> None:
        self.left = MAX_CHANGED_LINES

    def spend(self, lines: int) -> None:
        self.left -= lines
        if self.left < 0:
            raise DiffTooLargeError(
                f"the diff has over {MAX_CHANGED_LINES} changed lines", MAX_CHANGED_LINES
            )


def _hunk(header: str, lines: _Lines, budget: _Budget) -> Hunk:
    """The hunk under ``header``. Its counts say where it ends: a removed line may itself
    start with ``---``. Consecutive lines of one kind are kept as one run."""
    match = _HUNK_HEADER.match(header)
    if match is None:
        raise DiffParseError(f"unreadable hunk header: {header[:80]!r}")
    old_number, new_number = int(match.group(1)), int(match.group(3))
    old_count = 1 if match.group(2) is None else int(match.group(2))
    new_count = 1 if match.group(4) is None else int(match.group(4))
    old_left, new_left = old_count, new_count
    runs: list[Run] = []
    kind: str | None = None
    start = anchor = 0

    def close(end: int) -> None:
        if kind is not None:
            runs.append(Run("+" if kind == "+" else "-", start, end, anchor))
            budget.spend(end - start + 1)

    while old_left or new_left:
        line = lines.take()
        if line is None:
            raise DiffParseError("a hunk ends before its header's line counts")
        marker = line[:1]
        if marker == "\\":
            continue  # "\ No newline at end of file"
        if marker not in ("+", "-") or marker != kind:
            close((new_number if kind == "+" else old_number) - 1)
            kind = None
        if marker == " " and old_left and new_left:
            old_number, new_number = old_number + 1, new_number + 1
            old_left, new_left = old_left - 1, new_left - 1
        elif marker == "-" and old_left:
            if kind is None:
                kind, start, anchor = "-", old_number, max(new_number, 1)
            old_number, old_left = old_number + 1, old_left - 1
        elif marker == "+" and new_left:
            if kind is None:
                kind, start, anchor = "+", new_number, new_number
            new_number, new_left = new_number + 1, new_left - 1
        else:
            raise DiffParseError(f"unexpected line in a hunk: {line[:80]!r}")
    close((new_number if kind == "+" else old_number) - 1)
    while (following := lines.peek()) is not None and following.startswith("\\"):
        lines.take()
    return Hunk(int(match.group(1)), old_count, int(match.group(3)), new_count, tuple(runs))


def parse_unified_diff(text: str) -> ParsedDiff:
    """Parse ``git diff`` output (the workspace's pinned format: ``a/``/``b/`` prefixes,
    renames detected, binaries as ``Binary files … differ``).

    Raises:
        DiffParseError: the text is not a unified diff, or a hunk is cut short.
        DiffTooLargeError: it changes over ``MAX_CHANGED_LINES`` lines or ``MAX_FILES``
            files (found while reading, before anything past the cap is built).
    """
    lines = _Lines(text)
    budget = _Budget()
    files: list[FileDiff] = []
    while (line := lines.take()) is not None:
        if not line.startswith(_DIFF_HEADER):
            raise DiffParseError(f"expected a diff --git header: {line[:80]!r}")
        if len(files) >= MAX_FILES:
            raise DiffTooLargeError(f"the diff changes over {MAX_FILES} files", MAX_FILES)
        old, new = _header_paths(line.removeprefix(_DIFF_HEADER))
        builder = _FileBuilder(old, new)
        while (following := lines.peek()) is not None and not following.startswith(
            (_DIFF_HEADER, "@@")
        ):
            builder.header(following)
            lines.take()
        while (following := lines.peek()) is not None and following.startswith("@@"):
            lines.take()
            builder.hunks.append(_hunk(following, lines, budget))
        files.append(builder.build())
    return ParsedDiff(tuple(files))


def hunk_index(diff: ParsedDiff, limit: int = HUNK_INDEX_CHARS) -> HunkIndex:
    """The hunks of ``diff`` as agents must cite them: each file (its path JSON-quoted, as
    PR content) with every hunk's index, header and changed-line counts, in Jesinia's own
    numbering. A file whose listing would take over ``limit // SUMMARY_SHARE`` characters
    (a lockfile's thousands of hunks), or more than is left, is summarised in one line
    (its hunk count and index range), so one huge file never hides the files after it; a
    file even its summary doesn't fit is left out, and counted."""
    parts: list[str] = []
    used = listed = summarised = 0
    for position, diff_file in enumerate(diff.files):
        path = json.dumps(diff_file.path, ensure_ascii=False)
        block = _index_block(diff, position, path)
        if len(block) > limit // SUMMARY_SHARE or used + len(block) + 1 > limit:
            first = diff.cited_index(position, 0)
            last = first + len(diff_file.hunks) - 1
            block = (
                f"{path} ({diff_file.status}: {len(diff_file.hunks)} hunks, {first} to {last}, "
                "too many to list here)"
            )
            if used + len(block) + 1 > limit:
                continue
            summarised += 1
        parts.append(block)
        used += len(block) + 1
        listed += 1
    return HunkIndex("\n".join(parts), listed, len(diff.files), summarised)


def _index_block(diff: ParsedDiff, position: int, path: str) -> str:
    diff_file = diff.files[position]
    if diff_file.hunks:
        entry = [f"{path} ({diff_file.status})"]
        for h, hunk in enumerate(diff_file.hunks):
            added = sum(run.count for run in hunk.runs if run.kind == "+")
            cited = diff.cited_index(position, h)
            entry.append(f"  hunk {cited}: {hunk.header} (+{added} -{hunk.changed - added})")
        return "\n".join(entry)
    if diff_file.status == "renamed":
        what = f"renamed from {json.dumps(diff_file.old_path, ensure_ascii=False)}"
    else:
        what = f"{diff_file.status}, binary" if diff_file.is_binary else diff_file.status
    return f"{path} ({what}: no hunks)"


@dataclass(frozen=True)
class HunkIndex:
    """The hunk index text, how many of the diff's files it lists, and how many of those
    it lists only as a one-line summary (no per-hunk headers)."""

    text: str
    files_listed: int
    files_total: int
    files_summarised: int = 0

    @property
    def truncated(self) -> bool:
        return self.files_listed < self.files_total


# --- coverage --------------------------------------------------------------------------------

# Covered changed lines per hunk, keyed by (file position, hunk index).
_Covered = dict[tuple[int, int], int]


def _percent(covered: int, total: int) -> float:
    return round(100.0 * covered / total, 1)


def _agent_reasons(
    diff: ParsedDiff, agent_skips: Sequence[Skip]
) -> dict[tuple[str, int | None], str]:
    reasons: dict[tuple[str, int | None], str] = {}
    for skip in agent_skips:
        known = diff.has_path(skip.path)
        valid_hunk = skip.hunk is None or diff.locate(skip.path, skip.hunk) is not None
        if known and valid_hunk and skip.reason in AGENT_SKIP_REASONS:
            reasons.setdefault((skip.path, skip.hunk), skip.reason)
    return reasons


def _summarise(
    diff: ParsedDiff, covered: _Covered, agent_skips: Sequence[Skip], dropped_steps: int
) -> Coverage:
    reasons = _agent_reasons(diff, agent_skips)
    not_walked: list[Skip] = []
    total_not_walked = files_covered = auto_skipped = 0
    deleted_lines = deleted_covered = 0

    def left_out(skip: Skip) -> None:
        nonlocal total_not_walked
        total_not_walked += 1
        if len(not_walked) < NOT_WALKED_LIMIT:
            not_walked.append(skip)

    for f, diff_file in enumerate(diff.files):
        walked = [covered.get((f, h), 0) for h in range(len(diff_file.hunks))]
        auto = None if any(walked) else _auto_reason(diff_file)
        if diff_file.status == "deleted" and auto is None:
            deleted_lines += diff_file.changed_lines
            deleted_covered += sum(walked)
        if diff_file.hunks and all(
            count == hunk.changed for count, hunk in zip(walked, diff_file.hunks, strict=True)
        ):
            files_covered += 1
        elif auto is not None:
            auto_skipped += diff_file.changed_lines
            left_out(Skip(diff_file.path, None, auto))
        elif (diff_file.path, None) in reasons and not any(walked):
            left_out(Skip(diff_file.path, None, reasons[(diff_file.path, None)]))
        else:
            for h, hunk in enumerate(diff_file.hunks):
                if walked[h] < hunk.changed:
                    cited = diff.cited_index(f, h)
                    reason = reasons.get((diff_file.path, cited), "omitted")
                    left_out(Skip(diff_file.path, cited, reason))
    lines_total, files_total = diff.changed_lines, len(diff.files)
    if not diff.complete:
        lines_total = max(lines_total, diff.expected_lines)
        files_total = max(files_total, diff.expected_files)
    lines_covered = sum(covered.values())
    # Nothing changed line by line (binaries, renames): nothing was left unread.
    pct = _percent(lines_covered, lines_total) if lines_total else (100.0 if diff.complete else 0.0)
    reviewable = lines_total - auto_skipped - deleted_lines
    reviewable_pct = (
        _percent(lines_covered - deleted_covered, reviewable)
        if reviewable
        else (100.0 if diff.complete else 0.0)
    )
    if not diff.complete:
        pct, reviewable_pct = min(pct, INCOMPLETE_CEILING), min(reviewable_pct, INCOMPLETE_CEILING)
    return Coverage(
        pct=pct,
        files_covered=files_covered,
        files_total=files_total,
        not_walked=not_walked,
        lines_covered=lines_covered,
        lines_total=lines_total,
        lines_auto_skipped=auto_skipped,
        reviewable_pct=reviewable_pct,
        dropped_steps=dropped_steps,
        complete=diff.complete,
        incomplete_reason=diff.incomplete_reason,
        not_walked_total=total_not_walked,
    )


def _auto_reason(diff_file: FileDiff) -> str | None:
    """Jesinia's own reason to leave a whole file out (the shared skip list first)."""
    reason = auto_skip_reason(diff_file.path, diff_file.is_binary)
    if reason is not None or diff_file.hunks:
        return reason
    return "rename_only" if diff_file.status == "renamed" else "metadata_only"


def coverage(diff: ParsedDiff, steps: list[Step], skipped: list[Skip]) -> Coverage:
    """A walkthrough's coverage of ``diff``: each step covers every changed line of the hunks
    it cites (numbered per path, across a type change's two entries). A step citing a hunk
    the diff doesn't have (or none) is dropped, and counted.
    ``skipped`` is the agent's list of what it left out; entries for unknown files or hunks,
    or with a reason it may not give, are ignored."""
    walked: set[tuple[int, int]] = set()
    dropped = 0
    for step in steps:
        found = [diff.locate(ref.path, ref.hunk) for ref in step.hunks]
        cited = {slot for slot in found if slot is not None}
        if not step.hunks or None in found:
            dropped += 1
            continue
        walked |= cited
    covered: _Covered = {(f, h): diff.files[f].hunks[h].changed for f, h in walked}
    return _summarise(diff, covered, skipped, dropped)


def _merged(ranges: Sequence[LineRange]) -> tuple[list[int], list[int]]:
    """``ranges`` sorted and merged into disjoint intervals, as parallel start/end lists."""
    starts: list[int] = []
    ends: list[int] = []
    for item in sorted(ranges, key=lambda r: r.start):
        if ends and item.start <= ends[-1] + 1:
            ends[-1] = max(ends[-1], item.end)
        else:
            starts.append(item.start)
            ends.append(item.end)
    return starts, ends


def _overlap(intervals: tuple[list[int], list[int]], start: int, end: int) -> int:
    """How many of the lines ``start``..``end`` the merged intervals contain."""
    starts, ends = intervals
    total = 0
    position = bisect_left(ends, start)
    while position < len(starts) and starts[position] <= end:
        total += min(end, ends[position]) - max(start, starts[position]) + 1
        position += 1
    return total


def verified_ranges(claimed: Sequence[LineRange], read: Sequence[LineRange]) -> list[LineRange]:
    """The parts of the ``claimed`` ranges that were also ``read``: what a correctness pass
    says it checked, kept only where its tools actually returned those lines (M7). Lines
    read but never claimed don't count either. Sorted by path, then line."""
    read_by_path: dict[str, list[LineRange]] = {}
    for item in read:
        read_by_path.setdefault(item.path, []).append(item)
    claimed_by_path: dict[str, list[LineRange]] = {}
    for item in claimed:
        claimed_by_path.setdefault(item.path, []).append(item)
    verified: list[LineRange] = []
    for path in sorted(claimed_by_path.keys() & read_by_path.keys()):
        read_starts, read_ends = _merged(read_by_path[path])
        for start, end in zip(*_merged(claimed_by_path[path]), strict=True):
            position = bisect_left(read_ends, start)
            while position < len(read_starts) and read_starts[position] <= end:
                low, high = max(start, read_starts[position]), min(end, read_ends[position])
                verified.append(LineRange(path, low, high))
                position += 1
    return verified


def checked_coverage(diff: ParsedDiff, checked: Sequence[LineRange]) -> Coverage:
    """The correctness pass's coverage: the changed lines inside the ranges it read. Ranges
    are in new-file lines (a removed run counts when the new line it sat in front of was
    read), except in a deleted file, which only has old lines."""
    by_path: dict[str, list[LineRange]] = {}
    for item in checked:
        by_path.setdefault(item.path, []).append(item)
    covered: _Covered = {}
    for f, diff_file in enumerate(diff.files):
        if diff_file.path not in by_path:
            continue
        intervals = _merged(by_path[diff_file.path])
        for h, hunk in enumerate(diff_file.hunks):
            count = 0
            for run in hunk.runs:
                if run.kind == "+" or diff_file.status == "deleted":
                    count += _overlap(intervals, run.start, run.end)
                elif _overlap(intervals, run.anchor, run.anchor):
                    count += run.count
            if count:
                covered[(f, h)] = count
    return _summarise(diff, covered, [], 0)


# --- agent output ----------------------------------------------------------------------------


def _int(value: object) -> int | None:
    """A JSON integer: never a bool, and never a float (``1e308`` is not a line number)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _items(data: object, key: str) -> list[Mapping[str, object]]:
    items = data.get(key) if isinstance(data, Mapping) else None
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _hunk_refs(hunks: object) -> tuple[HunkRef, ...]:
    """The hunks a step cites; empty (so the step is dropped) if any is malformed."""
    if not isinstance(hunks, list):
        return ()
    refs: list[HunkRef] = []
    for item in hunks:
        path = item.get("path") if isinstance(item, Mapping) else None
        index = _int(item.get("hunk")) if isinstance(item, Mapping) else None
        if not isinstance(path, str) or index is None:
            return ()
        refs.append(HunkRef(path, index))
    return tuple(refs)


def steps_from_json(data: object) -> list[Step]:
    """The steps of a ``walkthrough`` artifact (``{"steps": [{"hunks": [...]}]}``)."""
    steps: list[Step] = []
    for item in _items(data, "steps"):
        title = item.get("title")
        steps.append(Step(_hunk_refs(item.get("hunks")), title if isinstance(title, str) else ""))
    return steps


def skips_from_json(data: object) -> list[Skip]:
    """The well-formed entries of a ``walkthrough`` artifact's ``skipped`` list."""
    skips: list[Skip] = []
    for item in _items(data, "skipped"):
        path, hunk, reason = item.get("path"), item.get("hunk"), item.get("reason")
        index = None if hunk is None else _int(hunk)
        if (
            isinstance(path, str)
            and isinstance(reason, str)
            and (hunk is None or index is not None)
        ):
            skips.append(Skip(path, index, reason))
    return skips


def ranges_from_json(data: object, key: str = "checked") -> list[LineRange]:
    """The well-formed ranges under ``key``: a ``correctness`` artifact's ``checked`` (the
    agent's claim), or Jesinia's ``correctness_reads`` record's ``read``."""
    ranges: list[LineRange] = []
    for item in _items(data, key):
        path, start, end = (
            item.get("path"),
            _int(item.get("start_line")),
            _int(item.get("end_line")),
        )
        if isinstance(path, str) and start is not None and end is not None and start <= end:
            ranges.append(LineRange(path, start, end))
    return ranges
