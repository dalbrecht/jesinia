# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when any measured file is below its coverage minimum.

Standards (AGENTS.md "Testing"): Domain / Core 100%; Application, Infrastructure, and
everything else 95%.

Usage: python scripts/coverage_per_file.py COVERAGE_JSON [MINIMUM]
``COVERAGE_JSON`` is the output of ``pytest --cov --cov-report=json:PATH``. ``MINIMUM``
overrides the 95% default for non-domain, non-core files.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_MINIMUM = 95.0
STRICT_MINIMUM = 100.0


def minimum_for(name: str, default: float = DEFAULT_MINIMUM) -> float:
    """100% for ``jesinia/core/`` and any ``domain/`` package; ``default`` otherwise."""
    norm = name.replace("\\", "/")
    if norm.startswith("jesinia/core/") or "/domain/" in norm:
        return STRICT_MINIMUM
    return default


def below_minimum(report: Mapping[str, object], minimum: float) -> list[tuple[str, float]]:
    files = report.get("files", {})
    if not isinstance(files, Mapping):
        raise ValueError("coverage report has no 'files' table")
    low: list[tuple[str, float]] = []
    for name, data in sorted(files.items()):
        summary = data["summary"] if isinstance(data, Mapping) else {}
        pct = float(summary.get("percent_covered", 0.0))
        if pct < minimum_for(name, minimum):
            low.append((name, pct))
    return low


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    minimum = float(args[1]) if len(args) > 1 else DEFAULT_MINIMUM
    report = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    low = below_minimum(report, minimum)
    for name, pct in low:
        print(f"coverage {pct:.1f}% < {minimum_for(name, minimum):.0f}%: {name}")
    return 1 if low else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
