# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inline ``#tag`` grammar, rewriting and near-match suggestions for KB pages."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

TAG_LINE_PREFIX = "Tags:"

# A tag starts with a letter or digit (never "_"), continues with letters, digits or "-", and
# cannot hug a preceding word character, "/" or another "#" (so "C#", URLs and "##" are safe).
_TAG = re.compile(r"(?<![\w/#])#([^\W_](?:[^\W_]|-)*)")
_HEADING_LINE = re.compile(r" {0,3}#{1,6}(?:\s|$)")
_FENCE_LINE = re.compile(r" {0,3}(```|~~~)")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
_MIN_FUZZY_LETTERS = 5
_MAX_EDIT_DISTANCE = 2


def normalise_tag(raw: str) -> str:
    """Canonical tag form: no leading ``#``, lowercase, spaces/underscores as ``-``, NFC."""
    text = raw.strip().removeprefix("#").lower()
    text = re.sub(r"[\s_]+", "-", text)
    return unicodedata.normalize("NFC", text)


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _mask_ignored(body: str) -> str:
    """Blank out fenced code, headings and inline code, keeping offsets identical."""
    masked_lines: list[str] = []
    open_fence: str | None = None
    for line in body.splitlines(keepends=True):
        fence = _FENCE_LINE.match(line)
        if open_fence is not None:
            if fence is not None and fence.group(1) == open_fence:
                open_fence = None
            masked_lines.append(_blank(line))
        elif fence is not None:
            open_fence = fence.group(1)
            masked_lines.append(_blank(line))
        elif _HEADING_LINE.match(line):
            masked_lines.append(_blank(line))
        else:
            masked_lines.append(_INLINE_CODE.sub(lambda m: _blank(m.group(0)), line))
    return "".join(masked_lines)


def extract_tags(body: str) -> list[str]:
    """Normalised, unique tags in order of first appearance."""
    seen: dict[str, None] = {}
    for match in _TAG.finditer(_mask_ignored(body)):
        seen.setdefault(normalise_tag(match.group(1)), None)
    return list(seen)


def replace_tag(body: str, old: str, new: str) -> str:
    """Rewrite grammar-valid occurrences of ``old`` (case-insensitive) as ``#new``."""
    old_key = normalise_tag(old)
    replacement = "#" + normalise_tag(new)
    pieces: list[str] = []
    cursor = 0
    for match in _TAG.finditer(_mask_ignored(body)):
        if normalise_tag(match.group(1)) == old_key:
            pieces.append(body[cursor : match.start()])
            pieces.append(replacement)
            cursor = match.end()
    pieces.append(body[cursor:])
    return "".join(pieces)


def render_tag_line(tags: Sequence[str]) -> str:
    """The ``Tags: #a #b`` line appended to a page."""
    return " ".join([TAG_LINE_PREFIX, *(f"#{tag}" for tag in tags)])


def _levenshtein(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(previous[column] + 1, current[column - 1] + 1, previous[column - 1] + cost)
            )
        previous = current
    return previous[-1]


def _is_near(left: str, right: str) -> bool:
    left_bare = left.replace("-", "")
    right_bare = right.replace("-", "")
    is_same_after_plural = left.removesuffix("s") == right.removesuffix("s")
    is_fuzzy_candidate = min(len(left_bare), len(right_bare)) >= _MIN_FUZZY_LETTERS
    return (
        is_same_after_plural
        or left_bare == right_bare
        or (is_fuzzy_candidate and _levenshtein(left_bare, right_bare) <= _MAX_EDIT_DISTANCE)
    )


def near_matches(tag: str, known: Iterable[str]) -> list[str]:
    """Known tags that are probably the same tag, spelled differently (exact counts)."""
    wanted = normalise_tag(tag)
    return [candidate for candidate in known if _is_near(wanted, normalise_tag(candidate))]
