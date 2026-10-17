# SPDX-License-Identifier: AGPL-3.0-or-later
"""When a pre-review may start (spec §2 "Gates"), and how deep it proposes to go (§3.1).

Pure: the PR's cached state and the clock in, a decision out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

CiState = Literal["pending", "passing", "failing", "none"]
GateState = Literal[
    "fetch_failed", "waiting_ci", "ci_failing", "waiting_auto_review", "debouncing", "ready"
]
Depth = Literal["quick", "deep"]

# A new head waits this long without another push before a background run (builder's call).
DEBOUNCE = timedelta(minutes=10)
# How long to wait for a configured automated reviewer to land on the head.
AUTO_REVIEW_TIMEOUT = timedelta(minutes=30)
# Deep when a PR changes at least this much, or the author knows the area this little.
DEEP_LINES = 200
DEEP_FILES = 10
LOW_FAMILIARITY = 0.4

_CI_STATES: frozenset[str] = frozenset({"pending", "passing", "failing", "none"})


class GateInputError(ValueError):
    """A gate or depth input that cannot describe a real PR."""


@dataclass(frozen=True)
class GateSettings:
    """The waits the gates apply; loaded PRs skip the debounce (``debounce=0``)."""

    debounce: timedelta = DEBOUNCE
    auto_review_timeout: timedelta = AUTO_REVIEW_TIMEOUT

    def __post_init__(self) -> None:
        if self.debounce < timedelta(0) or self.auto_review_timeout < timedelta(0):
            raise GateInputError("gate waits can't be negative")


DEFAULT_GATES = GateSettings()


@dataclass(frozen=True)
class PrState:
    """What the gates read about a PR's current head.

    ``fetch_error`` is set when GitHub couldn't load the PR on the last sync: its cached
    fields may be stale, so it is never ready.
    """

    ci_state: CiState
    automated_reviewer: str | None
    auto_review_landed: bool
    last_head_change_at: datetime
    fetch_error: str | None = None


@dataclass(frozen=True)
class GateResult:
    state: GateState
    reason: str


def _minutes(span: timedelta) -> int:
    return max(1, -(-int(span.total_seconds()) // 60))  # round up: "1 min" until it's done


def gate(pr: PrState, now: datetime, setting: GateSettings = DEFAULT_GATES) -> GateResult:
    """Decide whether a pre-review may start on ``pr``'s head at ``now``.

    In order: the PR must have loaded, CI on the head must pass (a head with no checks yet
    waits too), a configured automated reviewer must have reviewed the head or timed out,
    and the head must have been quiet for the debounce.

    Raises:
        GateInputError: ``pr.ci_state`` is not a known CI state.
    """
    if pr.ci_state not in _CI_STATES:
        raise GateInputError(f"unknown CI state {pr.ci_state!r}")
    if pr.fetch_error is not None:
        return GateResult("fetch_failed", f"couldn't load: {pr.fetch_error}")
    if pr.ci_state == "failing":
        return GateResult("ci_failing", "CI is failing on this head; Review anyway to go ahead")
    if pr.ci_state == "pending":
        return GateResult("waiting_ci", "waiting for CI on this head")
    if pr.ci_state == "none":
        return GateResult("waiting_ci", "no CI checks reported on this head yet")
    since_push = now - pr.last_head_change_at
    reviewed = "CI passing"
    if pr.automated_reviewer is not None:
        if pr.auto_review_landed:
            reviewed += f"; {pr.automated_reviewer} reviewed this head"
        elif since_push < setting.auto_review_timeout:
            left = _minutes(setting.auto_review_timeout - since_push)
            return GateResult(
                "waiting_auto_review",
                f"waiting for {pr.automated_reviewer} to review ({left} min until timeout)",
            )
        else:
            reviewed += f"; timed out waiting for {pr.automated_reviewer}"
    if since_push < setting.debounce:
        left = _minutes(setting.debounce - since_push)
        return GateResult("debouncing", f"new commits; waiting {left} min for pushes to settle")
    return GateResult("ready", reviewed)


def propose_depth(lines: int, files: int, familiarity: float | None) -> Depth:
    """Deep for at least ``DEEP_LINES`` changed lines or ``DEEP_FILES`` files, or a
    familiarity below ``LOW_FAMILIARITY`` (0 to 1; ``None`` when unknown); else quick.

    Raises:
        GateInputError: negative sizes, or a familiarity outside 0 to 1.
    """
    if lines < 0 or files < 0:
        raise GateInputError(f"sizes can't be negative: {lines} lines, {files} files")
    if familiarity is not None and not 0.0 <= familiarity <= 1.0:
        raise GateInputError(f"familiarity must be between 0 and 1, got {familiarity}")
    if lines >= DEEP_LINES or files >= DEEP_FILES:
        return "deep"
    if familiarity is not None and familiarity < LOW_FAMILIARITY:
        return "deep"
    return "quick"
