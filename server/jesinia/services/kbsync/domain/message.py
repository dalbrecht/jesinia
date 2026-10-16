# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auto-sync commit messages: ``kb: 3 updated, 1 added (Sources/…)`` + trailer."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Code = Literal["A", "M", "D", "R"]
TRAILER = "Jesinia-Auto-Sync: true"
MAX_SCOPES = 3
_ORDER: tuple[tuple[Code, str], ...] = (
    ("A", "added"),
    ("M", "updated"),
    ("D", "deleted"),
    ("R", "renamed"),
)


@dataclass(frozen=True)
class StatusEntry:
    code: Code
    path: str


def parse_name_status(raw: str) -> list[StatusEntry]:
    """Parse ``git diff --cached --name-status -M`` output (renames report the new path)."""
    entries: list[StatusEntry] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        kind, path = parts[0][:1], parts[-1]
        code: Code = {"A": "A", "D": "D", "R": "R", "C": "A"}.get(kind, "M")  # type: ignore[assignment]
        entries.append(StatusEntry(code, path))
    return entries


def _scope(path: str) -> str:
    head, sep, _ = path.partition("/")
    return f"{head}/…" if sep else head


def summarize_changes(entries: Sequence[StatusEntry]) -> str:
    if not entries:
        return "kb: no changes"
    counts = {code: sum(1 for e in entries if e.code == code) for code, _ in _ORDER}
    parts = [f"{counts[code]} {label}" for code, label in _ORDER if counts[code]]
    scopes: list[str] = []
    for e in entries:
        s = _scope(e.path)
        if s not in scopes:
            scopes.append(s)
    shown = ", ".join(scopes[:MAX_SCOPES]) + (", …" if len(scopes) > MAX_SCOPES else "")
    return f"kb: {', '.join(parts)} ({shown})"


def commit_message(entries: Sequence[StatusEntry]) -> str:
    return f"{summarize_changes(entries)}\n\n{TRAILER}\n"
