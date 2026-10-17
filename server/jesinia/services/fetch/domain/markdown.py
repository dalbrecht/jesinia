# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repair the one way extracted Markdown headings render wrongly (ruling R-REN1).

A heading whose only content is a link can come out of the extractor as an empty heading line
with the link on the next line, indented, which CommonMark reads as an empty heading plus an
indented code block. This pass is deliberately narrow: it merges an empty heading with a
following line of plain inline text (or drops it), and dedents a lone link line right after a
heading. It works line by line without regular expressions and never touches anything inside
a fence. It only moves text between lines and strips leading whitespace, so it cannot create
or change a link or image target.
"""

from __future__ import annotations

_FENCE_CHARACTERS = ("`", "~")
_MINIMUM_FENCE_LENGTH = 3
_MAXIMUM_FENCE_INDENT = 3
_MAXIMUM_HEADING_LEVEL = 6
_MAXIMUM_ORDERED_MARKER_DIGITS = 9
_CODE_INDENT = 4
_BULLET_MARKERS = ("-", "+", "*")
_ORDERED_DELIMITERS = (".", ")")
_THEMATIC_BREAK_CHARACTERS = ("-", "*", "_")
_BLOCK_START_CHARACTERS = (">", "|", "<")


def _indent_width(line: str) -> int:
    return len(line[: len(line) - len(line.lstrip(" \t"))].expandtabs(_CODE_INDENT))


def _fence_marker(line: str) -> str | None:
    """The run of backticks or tildes on a line that can open or close a fence (CommonMark:
    indented at most three spaces), if any."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > _MAXIMUM_FENCE_INDENT:
        return None
    for character in _FENCE_CHARACTERS:
        marker = stripped[: len(stripped) - len(stripped.lstrip(character))]
        if len(marker) >= _MINIMUM_FENCE_LENGTH:
            return marker
    return None


def _opening_fence(line: str) -> str | None:
    marker = _fence_marker(line)
    if marker is None:
        return None
    info = line.lstrip(" ")[len(marker) :]
    return None if marker[0] == "`" and "`" in info else marker


def _closes_fence(line: str, opening: str) -> bool:
    marker = _fence_marker(line)
    return (
        marker is not None
        and marker[0] == opening[0]
        and len(marker) >= len(opening)
        and not line.lstrip(" ")[len(marker) :].strip()
    )


def _heading_level(line: str) -> int:
    """The ATX heading level of a line starting in column 0, or 0 when it is no heading."""
    level = len(line) - len(line.lstrip("#"))
    if not 1 <= level <= _MAXIMUM_HEADING_LEVEL:
        return 0
    rest = line[level:]
    return level if not rest or rest[0] in " \t" else 0


def _is_empty_heading(line: str) -> bool:
    level = _heading_level(line)
    return level > 0 and not line[level:].strip().strip("#").strip()


def _closing_bracket(text: str) -> int:
    """Index of the ``]`` closing the ``[`` at index 0 (escapes and nesting honoured), or -1."""
    depth = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        depth += {"[": 1, "]": -1}.get(character, 0)
        if depth == 0:
            return index
        index += 1
    return -1


def _is_link_destination(destination: str) -> bool:
    """A link destination with no title: ``<...>`` without a further ``>``, or a bare URL
    with no whitespace and balanced parentheses."""
    if destination.startswith("<"):
        return destination.endswith(">") and ">" not in destination[1:-1].replace("\\>", "")
    depth = 0
    for character in destination.replace("\\(", "").replace("\\)", ""):
        if character.isspace():
            return False
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth < 0:
            return False
    return bool(destination) and depth == 0


def _is_only_a_link(line: str) -> bool:
    """Whether ``line`` (whitespace aside) is a single inline link ``[text](url)``."""
    text = line.strip()
    if not text.startswith("["):
        return False
    closing = _closing_bracket(text)
    return (
        closing > 0
        and text[closing + 1 : closing + 2] == "("
        and text.endswith(")")
        and _is_link_destination(text[closing + 2 : -1])
    )


def _is_list_item(text: str) -> bool:
    if text[0] in _BULLET_MARKERS:
        return text[1:2] in ("", " ", "\t")
    digits = len(text) - len(text.lstrip("0123456789"))
    if not 1 <= digits <= _MAXIMUM_ORDERED_MARKER_DIGITS:
        return False
    return text[digits : digits + 1] in _ORDERED_DELIMITERS and text[digits + 1 : digits + 2] in (
        "",
        " ",
        "\t",
    )


def _is_line_of(text: str, characters: tuple[str, ...]) -> bool:
    """A thematic break or setext underline: one repeated character, spaces allowed."""
    compact = text.replace(" ", "").replace("\t", "")
    return compact[0] in characters and compact == compact[0] * len(compact)


def _is_plain_inline_text(line: str) -> bool:
    """Whether ``line`` is a line of inline text an empty heading above it can take as its
    own: not blank, indented code, a heading, fence, list item, quote, table, HTML, thematic
    break or setext underline."""
    if not line.strip() or _indent_width(line) >= _CODE_INDENT:
        return False
    text = line.strip()
    return not (
        _heading_level(text)
        or _fence_marker(text)
        or _is_list_item(text)
        or text.startswith(_BLOCK_START_CHARACTERS)
        or _is_line_of(text, (*_THEMATIC_BREAK_CHARACTERS, "="))
    )


def _is_lone_line(lines: list[str], index: int) -> bool:
    """Whether the line before ``index`` ends its run: nothing, or a blank line, follows."""
    return index >= len(lines) or not lines[index].strip()


def normalize_markdown(markdown: str) -> str:
    """``markdown`` with no empty heading and no heading link misread as indented code.

    An empty ATX heading (``### ``) takes the next line as its text when that line is plain
    inline text or a lone link, and is dropped otherwise. A lone indented link line right
    after a column-0 heading is dedented. Everything else (fenced and indented code, lists,
    quotes, paragraph continuations) is left exactly as it is.

    >>> normalize_markdown("### \\n      [step 1](https://example.org/#s1)")
    '### [step 1](https://example.org/#s1)'
    """
    lines = markdown.split("\n")
    output: list[str] = []
    fence: str | None = None
    is_after_heading = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if fence is not None:
            output.append(line)
            if _closes_fence(line, fence):
                fence = None
            continue
        if is_after_heading and _is_only_a_link(line) and _is_lone_line(lines, index):
            line = line.strip()
        if _is_empty_heading(line):
            following = lines[index] if index < len(lines) else ""
            is_lone_link = _is_only_a_link(following) and _is_lone_line(lines, index + 1)
            if not (is_lone_link or _is_plain_inline_text(following)):
                is_after_heading = False
                continue
            line = f"{'#' * _heading_level(line)} {following.strip()}"
            index += 1
        output.append(line)
        fence = _opening_fence(line)
        is_after_heading = _heading_level(line) > 0
    return "\n".join(output)
