# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scaling checks that hold on a loaded shared CI runner: they compare how the time grows
from ``n`` to ``4n`` (linear work grows about 4x, quadratic about 16x) instead of holding a
tight wall-clock bound, with only a generous absolute ceiling against hangs."""

from __future__ import annotations

import time
from collections.abc import Callable

# A 4x larger input may take at most this many times as long: linear is about 4, quadratic
# about 16; the margin absorbs a noisy runner.
GROWTH_LIMIT = 10.0
# Below this, timings are noise: the larger run passes whatever the ratio.
NOISE_FLOOR = 0.02
CEILING = 10.0


def best_time(run: Callable[[], object], repeats: int = 3) -> float:
    """The fastest of ``repeats`` runs, in seconds (the least disturbed by the runner)."""
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        run()
        best = min(best, time.perf_counter() - started)
    return best


def assert_linear(make: Callable[[int], Callable[[], object]], n: int) -> None:
    """``make(size)`` returns the work at ``size``: the work at ``4n`` must take at most
    ``GROWTH_LIMIT`` times the work at ``n`` (or be under the noise floor), and finish well
    within ``CEILING`` seconds."""
    small = best_time(make(n))
    large = best_time(make(4 * n))
    assert large < CEILING, f"{4 * n} took {large:.2f}s"
    assert large <= max(GROWTH_LIMIT * small, NOISE_FLOOR), (
        f"{n} took {small:.4f}s but {4 * n} took {large:.4f}s: not linear"
    )
