# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import UTC, datetime
from typing import cast

import pytest

from jesinia.services.github.domain.issue import (
    COMMENT_BODY_LIMIT,
    CloseReason,
    InvalidCloseError,
    InvalidIssueReferenceError,
    InvalidIssueTextError,
    InvalidLabelError,
    LinkedPr,
    github_time,
    summarize_linked_prs,
    validate_close,
    validate_comment_body,
    validate_content_path,
    validate_database_id,
    validate_issue_body,
    validate_issue_number,
    validate_issue_title,
    validate_label_color,
    validate_label_description,
    validate_label_name,
    validate_label_names,
    validate_page_token,
)


def test_issue_numbers_and_database_ids_must_be_positive() -> None:
    assert validate_issue_number(3) == 3
    assert validate_database_id(7) == 7
    for bad in (0, -1):
        with pytest.raises(InvalidIssueReferenceError):
            validate_issue_number(bad)
        with pytest.raises(InvalidIssueReferenceError):
            validate_database_id(bad)
    with pytest.raises(InvalidIssueReferenceError):
        validate_issue_number(True)


def test_page_tokens_are_page_numbers() -> None:
    assert validate_page_token(None) == 1
    assert validate_page_token("4") == 4
    for bad in ("0", "-1", "x", "", "1.5"):
        with pytest.raises(InvalidIssueReferenceError):
            validate_page_token(bad)


def test_github_time_needs_a_timezone() -> None:
    assert github_time(datetime(2026, 1, 2, 3, 4, 5, 999, tzinfo=UTC)) == "2026-01-02T03:04:05Z"
    with pytest.raises(InvalidIssueReferenceError):
        github_time(datetime(2026, 1, 2))


@pytest.mark.parametrize("name", ["", "   ", "x" * 51, "bad\nname", " padded"])
def test_label_names_follow_githubs_limits(name: str) -> None:
    with pytest.raises(InvalidLabelError):
        validate_label_name(name)


def test_label_names_colors_and_descriptions() -> None:
    assert validate_label_name("priority: high") == "priority: high"
    assert validate_label_names(["a", "b", "a"]) == ("a", "b")
    with pytest.raises(InvalidLabelError):
        validate_label_names([])
    with pytest.raises(InvalidLabelError):
        validate_label_names(["a"] * 101 + [str(n) for n in range(100)])
    assert validate_label_color("A1b2C3") == "A1b2C3"
    with pytest.raises(InvalidLabelError):
        validate_label_color("fff")
    assert validate_label_description("d" * 100) == "d" * 100
    with pytest.raises(InvalidLabelError):
        validate_label_description("d" * 101)


def test_comment_and_issue_text_limits() -> None:
    assert validate_comment_body("x" * COMMENT_BODY_LIMIT)
    for bad in ("", " \n", "x" * (COMMENT_BODY_LIMIT + 1)):
        with pytest.raises(InvalidIssueTextError):
            validate_comment_body(bad)
    assert validate_issue_body("") == ""
    with pytest.raises(InvalidIssueTextError):
        validate_issue_body("x" * (COMMENT_BODY_LIMIT + 1))
    assert validate_issue_title("Fix it") == "Fix it"
    for bad in ("", "  ", "t" * 257):
        with pytest.raises(InvalidIssueTextError):
            validate_issue_title(bad)


def test_close_reasons() -> None:
    validate_close("completed", None)
    validate_close("not_planned", None)
    validate_close("duplicate", None)
    validate_close("duplicate", 9)
    with pytest.raises(InvalidCloseError):
        validate_close("completed", 9)
    with pytest.raises(InvalidCloseError):
        validate_close(cast(CloseReason, "reopened"), None)
    with pytest.raises(InvalidIssueReferenceError):
        validate_close("duplicate", 0)


def test_content_paths_stay_inside_the_repo() -> None:
    assert validate_content_path(".jesinia/labels.yml") == ".jesinia/labels.yml"
    for bad in ("", "/abs", "a/../b", "..", "a//b", "a/", "a\\b", "a\x00b", "./a"):
        with pytest.raises(InvalidIssueReferenceError):
            validate_content_path(bad)


def test_linked_pr_state_prefers_merged_then_open() -> None:
    def pr(state: str) -> LinkedPr:
        return LinkedPr("a/b", 1, state, "u")

    assert summarize_linked_prs([]) is None
    assert summarize_linked_prs([pr("closed")]) == "closed"
    assert summarize_linked_prs([pr("closed"), pr("open")]) == "open"
    assert summarize_linked_prs([pr("open"), pr("merged")]) == "merged"
