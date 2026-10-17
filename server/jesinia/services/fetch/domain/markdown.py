# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repair the two ways extracted Markdown renders wrongly (ruling R-REN1).

An extractor can leave a heading line empty with its text on the next line, indented, or
indent the first line of prose after a heading or a code fence. CommonMark reads both as an
empty heading plus an indented code block. This pass merges or drops empty headings and
dedents the single line that would open such a block. It works line by line without regular
expressions, and never touches anything inside a fence. It only moves text between lines and
strips leading whitespace, so it cannot create or change a link or image target.
"""

from __future__ import annotations

_FENCE_CHARACTERS = ("`", "~")
_MINIMUM_FENCE_LENGTH = 3
_MAXIMUM_HEADING_LEVEL = 6
_CODE_INDENT = 4


def _fence_marker(line: str) -> str | None:
    """The run of backticks or tildes that opens or closes a fence on ``line``, if any."""
    stripped = line.lstrip(" ")
    for character in _FENCE_CHARACTERS:
        marker = stripped[: len(stripped) - len(stripped.lstrip(character))]
        if len(marker) >= _MINIMUM_FENCE_LENGTH:
            return marker
    return None


def _closes_fence(line: str, opening: str) -> bool:
    marker = _fence_marker(line)
    return (
        marker is not None
        and marker[0] == opening[0]
        and len(marker) >= len(opening)
        and not line.strip()[len(marker) :].strip()
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


def _is_indented_code_start(line: str) -> bool:
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    return bool(line.strip()) and len(indent.expandtabs(_CODE_INDENT)) >= _CODE_INDENT


def _can_join_heading(line: str) -> bool:
    """Whether ``line`` is text an empty heading above it can take as its own."""
    return bool(line.strip()) and _heading_level(line) == 0 and _fence_marker(line) is None


def normalize_markdown(markdown: str) -> str:
    """``markdown`` with no empty heading and no prose misread as indented code.

    An empty ATX heading (``### ``) takes the text of the next line when that line is plain
    text, and is dropped otherwise. An indented line right after a column-0 heading or a
    column-0 closing fence is dedented. Fenced code, indented code after a blank line, list
    content and paragraph continuations are left exactly as they are.

    >>> normalize_markdown("### \\n      [step 1](https://example.org/#s1)")
    '### [step 1](https://example.org/#s1)'
    """
    lines = markdown.split("\n")
    output: list[str] = []
    fence: str | None = None
    # The previous line ended a block after which an indented line would open a code block.
    is_after_block_end = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if fence is not None:
            output.append(line)
            if _closes_fence(line, fence):
                fence = None
                is_after_block_end = not line.startswith((" ", "\t"))
            continue
        if is_after_block_end and _is_indented_code_start(line):
            line = line.strip()
        if _is_empty_heading(line):
            following = lines[index] if index < len(lines) else ""
            if not _can_join_heading(following):
                is_after_block_end = False
                continue
            line = f"{'#' * _heading_level(line)} {following.strip()}"
            index += 1
        output.append(line)
        fence = _fence_marker(line)
        is_after_block_end = fence is None and _heading_level(line) > 0
    return "\n".join(output)
