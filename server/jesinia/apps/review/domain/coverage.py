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
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from jesinia.apps.review.domain.skip import auto_skip_reason

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

_DIFF_HEADER = "diff --git "
_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
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
class ChangedLine:
    """An added (``+``, ``number`` in the new file) or removed (``-``, ``number`` in the old
    file) line. ``anchor`` is where it sits in the new file: the line number an added line
    has, or the new line a removed one was in front of."""

    kind: LineKind
    number: int
    anchor: int


@dataclass(frozen=True)
class Hunk:
    old_start: int
    new_start: int
    lines: tuple[ChangedLine, ...]


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
        return sum(len(hunk.lines) for hunk in self.hunks)


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

    def file(self, path: str) -> FileDiff | None:
        return next((f for f in self.files if f.path == path), None)


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
    Jesinia itself skips (lockfiles, vendored, generated, binaries); the evidence rule reads
    it. Neither reaches 100 for an incomplete diff.
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


def _hunk(lines: list[str], index: int) -> tuple[Hunk, int]:
    """The hunk whose header is ``lines[index]``, and the index after it. The header's
    counts say where it ends: a removed line may itself start with ``---``."""
    match = _HUNK_HEADER.match(lines[index])
    if match is None:
        raise DiffParseError(f"unreadable hunk header: {lines[index][:80]!r}")
    old_number, new_number = int(match.group(1)), int(match.group(3))
    old_left = 1 if match.group(2) is None else int(match.group(2))
    new_left = 1 if match.group(4) is None else int(match.group(4))
    start_old, start_new = old_number, new_number
    changed: list[ChangedLine] = []
    index += 1
    while old_left or new_left:
        if index >= len(lines):
            raise DiffParseError("a hunk ends before its header's line counts")
        line = lines[index]
        marker = line[:1]
        if marker == " " and old_left and new_left:
            old_number, new_number = old_number + 1, new_number + 1
            old_left, new_left = old_left - 1, new_left - 1
        elif marker == "-" and old_left:
            changed.append(ChangedLine("-", old_number, max(new_number, 1)))
            old_number, old_left = old_number + 1, old_left - 1
        elif marker == "+" and new_left:
            changed.append(ChangedLine("+", new_number, new_number))
            new_number, new_left = new_number + 1, new_left - 1
        elif marker != "\\":
            raise DiffParseError(f"unexpected line in a hunk: {line[:80]!r}")
        index += 1
    while index < len(lines) and lines[index].startswith("\\"):
        index += 1  # "\ No newline at end of file" after the hunk's last line
    return Hunk(start_old, start_new, tuple(changed)), index


def parse_unified_diff(text: str) -> ParsedDiff:
    """Parse ``git diff`` output (the workspace's pinned format: ``a/``/``b/`` prefixes,
    renames detected, binaries as ``Binary files … differ``).

    Raises:
        DiffParseError: the text is not a unified diff, or a hunk is cut short.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FileDiff] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith(_DIFF_HEADER):
            raise DiffParseError(f"expected a diff --git header: {line[:80]!r}")
        old, new = _header_paths(line.removeprefix(_DIFF_HEADER))
        builder = _FileBuilder(old, new)
        index += 1
        while index < len(lines) and not lines[index].startswith((_DIFF_HEADER, "@@")):
            builder.header(lines[index])
            index += 1
        while index < len(lines) and lines[index].startswith("@@"):
            hunk, index = _hunk(lines, index)
            builder.hunks.append(hunk)
        files.append(builder.build())
    return ParsedDiff(tuple(files))


# --- coverage --------------------------------------------------------------------------------

# A covered changed line: (path, hunk index, index among the hunk's changed lines).
_Covered = set[tuple[str, int, int]]


def _percent(covered: int, total: int) -> float:
    return round(100.0 * covered / total, 1)


def _summarise(
    diff: ParsedDiff, covered: _Covered, agent_skips: Sequence[Skip], dropped_steps: int
) -> Coverage:
    reasons: dict[tuple[str, int | None], str] = {}
    for skip in agent_skips:
        known = diff.file(skip.path)
        valid_hunk = skip.hunk is None or (known is not None and 0 <= skip.hunk < len(known.hunks))
        if known is not None and valid_hunk and skip.reason in AGENT_SKIP_REASONS:
            reasons.setdefault((skip.path, skip.hunk), skip.reason)
    not_walked: list[Skip] = []
    files_covered = auto_skipped = 0
    for diff_file in diff.files:
        walked = [
            sum((diff_file.path, h, i) in covered for i in range(len(hunk.lines)))
            for h, hunk in enumerate(diff_file.hunks)
        ]
        if diff_file.hunks and all(
            count == len(hunk.lines) for count, hunk in zip(walked, diff_file.hunks, strict=True)
        ):
            files_covered += 1
            continue
        auto = None if any(walked) else _auto_reason(diff_file)
        if auto is not None:
            auto_skipped += diff_file.changed_lines
            not_walked.append(Skip(diff_file.path, None, auto))
        elif (diff_file.path, None) in reasons and not any(walked):
            not_walked.append(Skip(diff_file.path, None, reasons[(diff_file.path, None)]))
        else:
            for h, hunk in enumerate(diff_file.hunks):
                if walked[h] < len(hunk.lines):
                    reason = reasons.get((diff_file.path, h), "omitted")
                    not_walked.append(Skip(diff_file.path, h, reason))
    lines_total, files_total = diff.changed_lines, len(diff.files)
    if not diff.complete:
        lines_total = max(lines_total, diff.expected_lines)
        files_total = max(files_total, diff.expected_files)
    lines_covered = len(covered)
    everything = 100.0 if diff.complete and files_covered == files_total else 0.0
    pct = _percent(lines_covered, lines_total) if lines_total else everything
    reviewable = lines_total - auto_skipped
    reviewable_pct = (
        _percent(lines_covered, reviewable) if reviewable else (100.0 if diff.complete else 0.0)
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
    )


def _auto_reason(diff_file: FileDiff) -> str | None:
    """Jesinia's own reason to leave a whole file out (the shared skip list first)."""
    reason = auto_skip_reason(diff_file.path, diff_file.is_binary)
    if reason is not None or diff_file.hunks:
        return reason
    return "rename_only" if diff_file.status == "renamed" else "metadata_only"


def coverage(diff: ParsedDiff, steps: list[Step], skipped: list[Skip]) -> Coverage:
    """A walkthrough's coverage of ``diff``: each step covers every changed line of the hunks
    it cites. A step citing a hunk the diff doesn't have (or none) is dropped, and counted.
    ``skipped`` is the agent's list of what it left out; entries for unknown files or hunks,
    or with a reason it may not give, are ignored."""
    known = {(f.path, h) for f in diff.files for h in range(len(f.hunks))}
    walked: set[tuple[str, int]] = set()
    dropped = 0
    for step in steps:
        cited = {(ref.path, ref.hunk) for ref in step.hunks}
        if not cited or not cited <= known:
            dropped += 1
            continue
        walked |= cited
    covered: _Covered = {
        (f.path, h, i)
        for f in diff.files
        for h, hunk in enumerate(f.hunks)
        if (f.path, h) in walked
        for i in range(len(hunk.lines))
    }
    return _summarise(diff, covered, skipped, dropped)


def checked_coverage(diff: ParsedDiff, checked: Sequence[LineRange]) -> Coverage:
    """The correctness pass's coverage: the changed lines inside the ranges it read. Ranges
    are in new-file lines (a removed line counts by where it was in the new file), except
    in a deleted file, which only has old lines."""
    covered: _Covered = set()
    for diff_file in diff.files:
        ranges = [(r.start, r.end) for r in checked if r.path == diff_file.path]
        for h, hunk in enumerate(diff_file.hunks):
            for i, line in enumerate(hunk.lines):
                where = line.number if diff_file.status == "deleted" else line.anchor
                if any(start <= where <= end for start, end in ranges):
                    covered.add((diff_file.path, h, i))
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


def ranges_from_json(data: object) -> list[LineRange]:
    """The well-formed ``checked`` ranges of a ``correctness`` artifact."""
    ranges: list[LineRange] = []
    for item in _items(data, "checked"):
        path, start, end = (
            item.get("path"),
            _int(item.get("start_line")),
            _int(item.get("end_line")),
        )
        if isinstance(path, str) and start is not None and end is not None and start <= end:
            ranges.append(LineRange(path, start, end))
    return ranges
