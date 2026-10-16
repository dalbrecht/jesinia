# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report source files that lack the project's SPDX license header.

Usage: python scripts/check_spdx.py [ROOT]
Exits 1 and lists the offending files when any header is missing.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

HEADER = "SPDX-License-Identifier: AGPL-3.0-or-later"
HEADER_WINDOW = 3
SKIP_DIRS = frozenset({".standards", "node_modules", ".venv", "dist", "static", ".git"})
CHECKED_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".css", ".sh"}
)
CHECKED_NAMES = frozenset({"setup"})


def _has_header(path: Path) -> bool:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(HEADER_WINDOW), fh, strict=False):
            if HEADER in line:
                return True
    return False


def _candidates(root: Path, suffixes: set[str], names: set[str]) -> Iterable[Path]:
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if path.is_file() and (path.suffix in suffixes or path.name in names):
            yield path


def missing_spdx(root: Path, suffixes: set[str], names: set[str] | None = None) -> list[Path]:
    """Return files under ``root`` whose first lines lack the SPDX header, sorted."""
    return sorted(p for p in _candidates(root, suffixes, names or set()) if not _has_header(p))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path.cwd()
    missing = missing_spdx(root, set(CHECKED_SUFFIXES), set(CHECKED_NAMES))
    for path in missing:
        print(f"missing SPDX header: {path.relative_to(root)}")
    return 1 if missing else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
