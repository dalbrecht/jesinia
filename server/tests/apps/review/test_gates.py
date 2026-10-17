# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pre-review gates and the proposed depth: pure functions of the PR and the clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from jesinia.apps.review.domain.gates import (
    AUTO_REVIEW_TIMEOUT,
    DEBOUNCE,
    CiState,
    GateInputError,
    GateSettings,
    GateState,
    PrState,
    gate,
    propose_depth,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
QUIET = NOW - DEBOUNCE  # the head has been still for exactly the debounce window
RECENT = NOW - timedelta(minutes=3)
LONG_AGO = NOW - AUTO_REVIEW_TIMEOUT
REVIEWER = "codex[bot]"


def _pr(
    ci: str,
    reviewer: str | None = None,
    landed: bool = False,
    head_changed: datetime = QUIET,
    fetch_error: str | None = None,
) -> PrState:
    return PrState(
        ci_state=cast(CiState, ci),  # the table spells states as text, one of them invalid
        automated_reviewer=reviewer,
        auto_review_landed=landed,
        last_head_change_at=head_changed,
        fetch_error=fetch_error,
    )


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        # CI pending / failing / passing, with no automated reviewer, after the debounce.
        (_pr("pending"), "waiting_ci"),
        # No checks at all: wait 10 minutes for CI to show up, then treat it as no CI (R28).
        (_pr("none", head_changed=RECENT), "waiting_ci"),
        (_pr("none"), "ready"),
        (_pr("none", REVIEWER, head_changed=QUIET), "waiting_auto_review"),
        (_pr("failing"), "ci_failing"),
        (_pr("passing"), "ready"),
        # × an automated reviewer configured: not landed, landed, timed out.
        (_pr("passing", REVIEWER, head_changed=QUIET), "waiting_auto_review"),
        (_pr("passing", REVIEWER, landed=True), "ready"),
        (_pr("passing", REVIEWER, head_changed=LONG_AGO), "ready"),
        (_pr("pending", REVIEWER, landed=True), "waiting_ci"),
        (_pr("failing", REVIEWER), "ci_failing"),
        # × the debounce: a head that moved 3 minutes ago is still settling.
        (_pr("passing", head_changed=RECENT), "debouncing"),
        (_pr("passing", REVIEWER, landed=True, head_changed=RECENT), "debouncing"),
        (_pr("pending", head_changed=RECENT), "waiting_ci"),
        (_pr("failing", head_changed=RECENT), "ci_failing"),
        # A PR GitHub couldn't load is never ready, whatever else it says.
        (_pr("passing", fetch_error="GitHub answered 403: nope"), "fetch_failed"),
        (_pr("passing", REVIEWER, landed=True, fetch_error="boom"), "fetch_failed"),
    ],
)
def test_gate_states(pr: PrState, expected: GateState) -> None:
    result = gate(pr, NOW, GateSettings())
    assert result.state == expected
    assert result.reason


def test_gate_reasons_say_why() -> None:
    assert "couldn't load" in gate(_pr("passing", fetch_error="boom"), NOW).reason
    assert "boom" in gate(_pr("passing", fetch_error="boom"), NOW).reason
    assert "Review anyway" in gate(_pr("failing"), NOW).reason
    assert "no CI checks" in gate(_pr("none", head_changed=RECENT), NOW).reason
    assert "No CI configured" in gate(_pr("none"), NOW).reason
    assert REVIEWER in gate(_pr("passing", REVIEWER), NOW).reason
    timed_out = gate(_pr("passing", REVIEWER, head_changed=LONG_AGO), NOW)
    assert "timed out" in timed_out.reason and REVIEWER in timed_out.reason
    assert "7 min" in gate(_pr("passing", head_changed=RECENT), NOW).reason


def test_gate_settings_can_skip_the_debounce() -> None:
    immediate = GateSettings(debounce=timedelta(0))
    assert gate(_pr("passing", head_changed=NOW), NOW, immediate).state == "ready"
    with pytest.raises(GateInputError):
        GateSettings(debounce=timedelta(seconds=-1))
    with pytest.raises(GateInputError):
        GateSettings(auto_review_timeout=timedelta(seconds=-1))
    with pytest.raises(GateInputError):
        GateSettings(no_ci_grace=timedelta(seconds=-1))


def test_a_loaded_pr_still_waits_for_ci_to_appear() -> None:
    immediate = GateSettings(debounce=timedelta(0))
    assert gate(_pr("none", head_changed=RECENT), NOW, immediate).state == "waiting_ci"
    assert gate(_pr("none", head_changed=QUIET), NOW, immediate).state == "ready"


def test_gate_refuses_naive_times() -> None:
    with pytest.raises(GateInputError, match="timezone"):
        gate(_pr("passing"), NOW.replace(tzinfo=None))
    with pytest.raises(GateInputError, match="timezone"):
        gate(_pr("passing", head_changed=QUIET.replace(tzinfo=None)), NOW)


def test_gate_rejects_an_unknown_ci_state() -> None:
    with pytest.raises(GateInputError, match="CI state"):
        gate(_pr("flaky"), NOW)


@pytest.mark.parametrize(
    ("lines", "files", "familiarity", "expected"),
    [
        (0, 0, None, "quick"),
        (199, 9, None, "quick"),
        (200, 1, None, "deep"),
        (10, 10, None, "deep"),
        (10, 1, 0.39, "deep"),
        (10, 1, 0.4, "quick"),
        (10, 1, 1.0, "quick"),
        (500, 1, 1.0, "deep"),
    ],
)
def test_propose_depth_thresholds(
    lines: int, files: int, familiarity: float | None, expected: str
) -> None:
    assert propose_depth(lines, files, familiarity) == expected


@pytest.mark.parametrize(
    ("lines", "files", "familiarity"), [(-1, 0, None), (0, -1, None), (0, 0, 1.5), (0, 0, -0.1)]
)
def test_propose_depth_rejects_impossible_inputs(
    lines: int, files: int, familiarity: float | None
) -> None:
    with pytest.raises(GateInputError):
        propose_depth(lines, files, familiarity)
