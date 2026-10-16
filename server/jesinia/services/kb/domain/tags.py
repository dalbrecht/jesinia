# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inline ``#tag`` grammar, rewriting and near-match suggestions for KB pages."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

TAG_LINE_PREFIX = "Tags:"


def _combining_mark_class() -> str:
    """A regex class body of every combining mark (Unicode category M*).

    Python's ``\\w`` excludes marks, so a decomposed (NFD) ``#cafe\u0301`` would otherwise stop
    at ``e``. Marks only live in planes 0, 1 and 14, so only those are scanned.
    """
    code_points = (*range(0x20000), *range(0xE0000, 0xE1000))
    marks = (chr(code) for code in code_points if unicodedata.category(chr(code))[0] == "M")
    return "".join(re.escape(mark) for mark in marks)


_MARKS = _combining_mark_class()
# One tag character: a letter or digit (never "_") plus any combining marks on it.
_TAG_CHAR = rf"[^\W_][{_MARKS}]*"
# Single hyphens join tag characters; a trailing hyphen is never part of the tag.
_TAG_BODY = rf"{_TAG_CHAR}(?:-?{_TAG_CHAR})*"
# A tag cannot hug a preceding word character, combining mark, "/", "#", "?", "=" or "&", and
# cannot be a Markdown link target "](#a)" (so "C#", URLs, link anchors, query strings like
# "?q=#a", "&#38;" and "##" are never tags; "(#a)" in prose is). It must not run on into "_", a
# word character or a mark, even across one hyphen, so "#machine_learning" and "#b_c" are not
# tags at all rather than the tags "machine" and "b". Matching runs on the original text (never a
# normalised copy) so match offsets are the text's own and a rename leaves everything else
# byte-identical; the captured tag is NFC-normalised by ``normalise_tag``.
_TAG = re.compile(rf"(?<![\w/#?=&{_MARKS}])(?<!\]\()#({_TAG_BODY})(?!-?[\w{_MARKS}])")
_VALID_TAG = re.compile(_TAG_BODY)
_HEADING_LINE = re.compile(r" {0,3}#{1,6}(?:\s|$)")
_FENCE_LINE = re.compile(r" {0,3}(`{3,}|~{3,})(?P<rest>.*)$", re.DOTALL)
_INDENTED_LINE = re.compile(r"(?: {4}|\t)")
_LIST_MARKER = re.compile(r"[ \t]*(?:[-*+]|\d+[.)])(?:[ \t]|$)")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
_MIN_FUZZY_LETTERS = 5
_MAX_EDIT_DISTANCE = 2


def normalise_tag(raw: str) -> str:
    """Canonical tag form: no leading ``#``, case-folded, spaces/underscores as ``-``, NFC.

    Case folding (not ``lower``) is the identity, so ``#straße`` and ``#STRASSE`` are one tag.
    """
    text = raw.strip().removeprefix("#").casefold()
    text = re.sub(r"[\s_]+", "-", text)
    return unicodedata.normalize("NFC", text)


def has_tag_grammar(raw: str) -> bool:
    """Whether ``raw`` (any case, no leading ``#``) is a single well-formed tag body."""
    return _VALID_TAG.fullmatch(raw) is not None


def is_valid_tag(tag: str) -> bool:
    """Whether ``tag`` is already normalised and fits the ``#tag`` grammar (no leading ``#``)."""
    return _VALID_TAG.fullmatch(tag) is not None and tag == normalise_tag(tag)


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _is_indented_code(line: str, may_start_code: bool) -> bool:
    """An indented code line: 4 spaces or a tab, after a blank line or more code, and not
    an indented list item."""
    return (
        may_start_code
        and _INDENTED_LINE.match(line) is not None
        and line.strip() != ""
        and _LIST_MARKER.match(line) is None
    )


def _closes_fence(candidate: re.Match[str], opening_run: str) -> bool:
    """A closing fence repeats the opener's character, is at least as long, and has no info."""
    run = candidate.group(1)
    return (
        run[0] == opening_run[0]
        and len(run) >= len(opening_run)
        and candidate.group("rest").strip() == ""
    )


def _mask_ignored(body: str) -> str:
    """Blank out fenced and indented code, headings and inline code, keeping offsets identical."""
    masked_lines: list[str] = []
    open_fence: str | None = None
    may_start_code = True  # at the start, after a blank line, or inside an indented block
    for line in body.splitlines(keepends=True):
        fence = _FENCE_LINE.match(line)
        is_code = False
        if open_fence is not None:
            if fence is not None and _closes_fence(fence, open_fence):
                open_fence = None
            masked_lines.append(_blank(line))
        elif fence is not None:
            open_fence = fence.group(1)
            masked_lines.append(_blank(line))
        elif _is_indented_code(line, may_start_code):
            is_code = True
            masked_lines.append(_blank(line))
        elif _HEADING_LINE.match(line):
            masked_lines.append(_blank(line))
        else:
            masked_lines.append(_INLINE_CODE.sub(lambda m: _blank(m.group(0)), line))
        may_start_code = open_fence is None and (is_code or line.strip() == "")
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
