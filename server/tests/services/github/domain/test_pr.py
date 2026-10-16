# SPDX-License-Identifier: AGPL-3.0-or-later
from typing import cast

import pytest

from jesinia.services.github.domain.pr import (
    Check,
    CheckStatus,
    DraftComment,
    InvalidDraftCommentError,
    InvalidPullRequestReferenceError,
    PullRequestDomainError,
    ReviewSide,
    check_status_from_commit_status,
    check_status_from_run,
    is_bot_account,
    rollup_checks,
    validate_pull_number,
    validate_repo,
    validate_sha,
)


@pytest.mark.parametrize(
    ("login", "account_type", "expected"),
    [
        ("dependabot[bot]", "Bot", True),
        ("renovate[bot]", None, True),
        ("copilot-pull-request-reviewer", "Bot", True),
        ("alice", "User", False),
        ("bot-fan", "User", False),
        ("ghost", None, False),
    ],
)
def test_should_detect_bot_accounts_by_type_or_login_suffix(
    login: str, account_type: str | None, expected: bool
) -> None:
    assert is_bot_account(login, account_type) is expected


def _check(status: str) -> Check:
    return Check(name=status, status=cast(CheckStatus, status), url=None)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "none"),
        (["passing", "passing"], "passing"),
        (["passing", "pending"], "pending"),
        (["pending", "failing", "passing"], "failing"),
    ],
)
def test_should_roll_up_checks_with_failing_over_pending_over_passing(
    statuses: list[str], expected: str
) -> None:
    checks = [_check(s) for s in statuses]
    rolled = rollup_checks(checks)
    assert rolled.state == expected and rolled.checks == checks


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("queued", None, "pending"),
        ("in_progress", None, "pending"),
        ("completed", "success", "passing"),
        ("completed", "neutral", "passing"),
        ("completed", "skipped", "passing"),
        ("completed", "failure", "failing"),
        ("completed", "timed_out", "failing"),
        ("completed", "cancelled", "failing"),
        ("completed", None, "failing"),
    ],
)
def test_should_map_check_run_status_and_conclusion(
    status: str, conclusion: str | None, expected: str
) -> None:
    assert check_status_from_run(status, conclusion) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [("success", "passing"), ("pending", "pending"), ("failure", "failing"), ("error", "failing")],
)
def test_should_map_commit_status_state(state: str, expected: str) -> None:
    assert check_status_from_commit_status(state) == expected


def test_should_accept_well_formed_references() -> None:
    assert validate_repo("acme/api.v2_x-y") == "acme/api.v2_x-y"
    assert validate_pull_number(7) == 7
    assert validate_sha("a" * 40) == "a" * 40


@pytest.mark.parametrize("repo", ["acme", "acme/api/x", "../etc", "acme/..", "acme/.", "a b/c", ""])
def test_should_reject_malformed_repo_names(repo: str) -> None:
    with pytest.raises(InvalidPullRequestReferenceError, match="repo"):
        validate_repo(repo)


@pytest.mark.parametrize("number", [0, -3])
def test_should_reject_non_positive_pull_numbers(number: int) -> None:
    with pytest.raises(InvalidPullRequestReferenceError, match="number"):
        validate_pull_number(number)


@pytest.mark.parametrize("sha", ["main", "abc", "z" * 40, "a" * 41])
def test_should_reject_non_hex_commit_shas(sha: str) -> None:
    with pytest.raises(InvalidPullRequestReferenceError, match="sha"):
        validate_sha(sha)


def test_should_accept_single_and_multi_line_draft_comments() -> None:
    single = DraftComment(path="src/a.py", line=3, side="RIGHT", body="x")
    multi = DraftComment(
        path="src/a.py", line=9, side="RIGHT", body="y", start_line=4, start_side="RIGHT"
    )
    assert single.start_line is None and single.start_side is None
    assert (multi.start_line, multi.start_side) == (4, "RIGHT")


@pytest.mark.parametrize(
    ("path", "line", "side", "body", "start_line", "start_side", "message"),
    [
        ("a.py", 5, "RIGHT", "b", 2, None, "start_side"),
        ("a.py", 5, "RIGHT", "b", None, "LEFT", "start_line"),
        ("a.py", 5, "RIGHT", "b", 9, "RIGHT", "before"),
        ("a.py", 0, "RIGHT", "b", None, None, "line"),
        ("", 5, "RIGHT", "b", None, None, "path"),
        ("a.py", 5, "RIGHT", "  ", None, None, "body"),
        ("a.py", 5, "MIDDLE", "b", None, None, "side"),
        ("a.py", 5, "RIGHT", "b", 2, "UP", "start_side"),
    ],
)
def test_should_reject_invalid_draft_comments(
    path: str,
    line: int,
    side: str,
    body: str,
    start_line: int | None,
    start_side: str | None,
    message: str,
) -> None:
    with pytest.raises(InvalidDraftCommentError, match=message) as caught:
        DraftComment(
            path=path,
            line=line,
            side=cast(ReviewSide, side),
            body=body,
            start_line=start_line,
            start_side=cast(ReviewSide | None, start_side),
        )
    assert isinstance(caught.value, PullRequestDomainError)
