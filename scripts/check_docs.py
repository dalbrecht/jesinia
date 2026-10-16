# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check the docs: relative links resolve, ADRs are complete, the KB format is exact.

Usage: python scripts/check_docs.py [ROOT]  (exits 1 and lists every problem)
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
ADR_FIELDS = ("**Status:**", "**Date:**", "## Context", "## Decision", "## Consequences")
KB_FORMAT_MUST_CONTAIN = (
    "<!-- jesinia:ai agent=<agent> model=<model> at=<ISO-8601 UTC, Z> session=<id> -->",
    "<!-- /jesinia:ai -->",
    "` [<id-prefix>]`",
    "Never a bare\n  ` 2`",
)


def markdown_files(root: Path) -> list[Path]:
    return sorted([root / "README.md", *(root / "docs").rglob("*.md")])


def links(text: str) -> Iterator[str]:
    for m in LINK.finditer(FENCE.sub("", text)):
        yield m.group(1)


def broken_links(root: Path) -> list[str]:
    problems = []
    for md in markdown_files(root):
        for target in links(md.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if not (md.parent / path).exists():
                problems.append(f"{md.relative_to(root)}: broken link {target}")
    return problems


def incomplete_adrs(root: Path) -> list[str]:
    problems = []
    for adr in sorted((root / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        text = adr.read_text(encoding="utf-8")
        missing = [f for f in ADR_FIELDS if f not in text]
        if missing:
            problems.append(f"{adr.relative_to(root)}: missing {', '.join(missing)}")
    return problems


def kb_format_problems(root: Path) -> list[str]:
    text = (root / "docs" / "conventions" / "kb-format.md").read_text(encoding="utf-8")
    return [
        f"docs/conventions/kb-format.md: must contain {needle!r}"
        for needle in KB_FORMAT_MUST_CONTAIN
        if needle not in text
    ]


def problems(root: Path) -> list[str]:
    return broken_links(root) + incomplete_adrs(root) + kb_format_problems(root)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    found = problems(Path(args[0]) if args else Path.cwd())
    for p in found:
        print(p)
    return 1 if found else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
