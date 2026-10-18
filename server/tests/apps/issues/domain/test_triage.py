# SPDX-License-Identifier: AGPL-3.0-or-later
"""Triage state (spec §6.2): who is in the Inbox, saved filters and the resumable sync cursor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA, Canonical
from jesinia.apps.issues.domain.triage import (
    SAVED_FILTERS,
    STALE_AFTER,
    InvalidFilterError,
    InvalidViewError,
    IssueFilters,
    PreviousTriage,
    SavedFilter,
    SyncCursor,
    content_hash,
    in_view,
    is_inbox,
    parse_saved_filter,
    parse_view,
    stale_before,
    triage,
)
from jesinia.services.github.domain.repo import InvalidRepoError

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
COMPLETE = ("agent-ready", "priority: high", "bug", "area:web")


def test_issue_opened_after_the_repo_was_added_is_in_the_inbox() -> None:
    assert is_inbox(None, created_at=T0 + timedelta(seconds=1), added_at=T0, complete=False)
    # GitHub's times have whole seconds; the watch time has microseconds. An issue opened in
    # the same second as the watch counts as new, one a second earlier as existing.
    watched_at = T0 + timedelta(microseconds=400_000)
    assert is_inbox(None, created_at=T0, added_at=watched_at, complete=False)
    assert not is_inbox(
        None, created_at=T0 - timedelta(seconds=1), added_at=watched_at, complete=False
    )
    assert not is_inbox(None, created_at=T0 - timedelta(days=9), added_at=T0, complete=True)


def test_becoming_incomplete_sends_an_issue_back_and_nothing_else_does() -> None:
    triaged = PreviousTriage(complete=True, inbox=False)
    assert is_inbox(triaged, created_at=T0, added_at=T0, complete=False)
    assert not is_inbox(triaged, created_at=T0, added_at=T0, complete=True)
    backlog = PreviousTriage(complete=False, inbox=False)
    assert not is_inbox(backlog, created_at=T0, added_at=T0, complete=False)
    assert not is_inbox(backlog, created_at=T0, added_at=T0, complete=True)
    inboxed = PreviousTriage(complete=False, inbox=True)
    assert is_inbox(inboxed, created_at=T0, added_at=T0, complete=False)
    # Complete means triaged: the flag clears, so a schema change can't bring it back.
    assert not is_inbox(inboxed, created_at=T0, added_at=T0, complete=True)
    assert not is_inbox(None, created_at=T0 + timedelta(seconds=1), added_at=T0, complete=True)


def test_inbox_view_hides_complete_issues_and_backlog_holds_the_rest() -> None:
    assert in_view("inbox", inbox=True, complete=False)
    assert not in_view("inbox", inbox=True, complete=True)
    assert not in_view("inbox", inbox=False, complete=False)
    assert in_view("backlog", inbox=False, complete=False)
    assert in_view("backlog", inbox=True, complete=True)
    assert not in_view("backlog", inbox=True, complete=False)


def test_triage_counts_sub_issues_as_an_epic() -> None:
    plain = triage(COMPLETE, 0, DEFAULT_SCHEMA)
    assert plain.complete and plain.missing == [] and not plain.is_epic
    epic = triage(("priority: low",), 3, DEFAULT_SCHEMA)
    assert epic.complete and epic.is_epic
    labelled = triage(("epic",), 0, DEFAULT_SCHEMA)
    assert labelled.is_epic and labelled.missing == ["priority"]
    conflicted = triage(
        ("bug", "test", "agent-ready", "priority: low", "area:x"), 0, DEFAULT_SCHEMA
    )
    assert not conflicted.complete and conflicted.conflicts == ["type: bug, test"]


def test_views_and_saved_filters_are_validated() -> None:
    assert parse_view("inbox") == "inbox" and parse_view("backlog") == "backlog"
    with pytest.raises(InvalidViewError):
        parse_view("epics")
    assert SAVED_FILTERS == (
        "incomplete",
        "stale_90d",
        "human_needed_could_be_ready",
        "possible_duplicates",
    )
    assert parse_saved_filter(None) is None
    assert parse_saved_filter("stale_90d") == "stale_90d"
    assert parse_saved_filter("possible_duplicates") == "possible_duplicates"
    with pytest.raises(InvalidFilterError):
        parse_saved_filter("duplicates")
    assert IssueFilters().repo is None
    assert IssueFilters(repo="acme/web", saved="incomplete").saved == "incomplete"
    with pytest.raises(InvalidRepoError):
        IssueFilters(repo="../x")
    with pytest.raises(InvalidFilterError):
        IssueFilters(saved=cast(SavedFilter, "nope"))


def test_stale_means_no_activity_in_90_days() -> None:
    assert STALE_AFTER == timedelta(days=90)
    assert stale_before(T0) == T0 - timedelta(days=90)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_cursor_restarts_from_the_high_water_mark_after_progress() -> None:
    cursor = SyncCursor()
    assert not cursor.covers(_at(0), 1)
    after, done = cursor.after_page([(_at(1), 10), (_at(2), 11), (_at(2), 12)], "2")
    assert not done
    assert after == SyncCursor(
        since=_at(2), page_token=None, high_water=_at(2), seen_ids=frozenset({11, 12})
    )
    assert after.covers(_at(1), 10) and after.covers(_at(2), 11)
    assert not after.covers(_at(2), 13) and not after.covers(_at(3), 11)


def test_cursor_pages_forward_when_a_page_makes_no_progress() -> None:
    cursor = SyncCursor(since=_at(2), page_token=None, high_water=_at(2), seen_ids=frozenset({1}))
    after, done = cursor.after_page([(_at(2), 2), (_at(2), 3)], "2")
    assert not done
    assert after == SyncCursor(_at(2), "2", _at(2), frozenset({1, 2, 3}))
    # A page with only pull requests (no issues) on a first sync pages forward too.
    # An issue already behind the mark (taken before) changes nothing.
    behind, done = after.after_page([(_at(1), 9)], "3")
    assert behind == SyncCursor(_at(2), "3", _at(2), frozenset({1, 2, 3}))
    empty, done = SyncCursor().after_page([], "2")
    assert not done and empty == SyncCursor(page_token="2")


def test_cursor_finishes_ready_for_the_next_incremental_sync() -> None:
    cursor = SyncCursor(since=_at(1), page_token="3", high_water=_at(1), seen_ids=frozenset({4}))
    after, done = cursor.after_page([(_at(5), 7)], None)
    assert done
    assert after == SyncCursor(_at(5), None, _at(5), frozenset({7}))
    nothing, done = SyncCursor().after_page([], None)
    assert done and nothing == SyncCursor()


def test_cursor_round_trips_through_json() -> None:
    cursor = SyncCursor(_at(1), "2", _at(3), frozenset({5, 4}))
    assert SyncCursor.from_json(cursor.to_json()) == cursor
    assert SyncCursor.from_json(SyncCursor().to_json()) == SyncCursor()


def test_alias_schema_completes_via_triage() -> None:
    schema = DEFAULT_SCHEMA.__class__(
        readiness=(Canonical("design-needed", ("design needed",)),),
        priority=DEFAULT_SCHEMA.priority,
        type=DEFAULT_SCHEMA.type,
    )
    assert triage(("design needed", "priority: low", "bug", "area:x"), 0, schema).complete


def test_content_hash_covers_title_body_and_labels_only() -> None:
    base = content_hash("Crash", "on login", ["bug", "area:web"])
    assert base == content_hash("Crash", "on login", ["Area:Web ", "bug"])  # order, case
    assert base != content_hash("Crash!", "on login", ["bug", "area:web"])
    assert base != content_hash("Crash", "on login.", ["bug", "area:web"])
    assert base != content_hash("Crash", "on login", ["bug"])
