# SPDX-License-Identifier: AGPL-3.0-or-later
"""Changes, their ordered series and the label edits they make (spec §6.2; plan Task 6)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from jesinia.apps.issues.domain.changes import (
    AddArea,
    BatchResult,
    Change,
    ChangeBatch,
    ChangedOnGitHubError,
    Close,
    Comment,
    CreatedIssue,
    CreateIssue,
    EnsureLabel,
    Existing,
    InvalidChangeError,
    MissingLabelError,
    New,
    PendingChange,
    RemoveArea,
    ReorderChild,
    SetDimension,
    SetParent,
    StepResult,
    approval_batch,
    batch_json,
    change_json,
    check_change,
    label_edit,
    labels_needed,
    parse_batch,
    parse_change,
    parse_issue_change,
    parse_result,
    parse_source,
    pending_batch,
    result_json,
    series,
)
from jesinia.apps.issues.domain.schema import (
    DEFAULT_SCHEMA,
    Canonical,
    SchemaEntry,
    parse_labels_yml,
    with_overlay,
)
from jesinia.apps.issues.domain.suggestions import Suggestion, parse_kind

REPO = "acme/web"
E1, E2, E5 = Existing(REPO, 1), Existing(REPO, 2), Existing(REPO, 5)
AMICUS = parse_labels_yml(
    "readiness:\n  - agent-ready\n  - name: design-needed\n    aliases: [design needed]\n"
    "areas:\n  - name: area:apple\n    aliases: [platform:ios, apple]\n"
)
ON_GITHUB = ("agent-ready", "design needed", "priority: high", "priority: low", "area:web", "apple")


def test_series_order_is_labels_parent_order_close_comment() -> None:
    batch = ChangeBatch.of(
        {
            E1: [
                Comment("b"),
                Close("completed"),
                ReorderChild(E2, None),
                SetParent(E5),
                AddArea("area:web"),
            ],
            New("t"): [Comment("c")],
        },
        repo=[
            CreateIssue("t", REPO, "T", "", ()),
            EnsureLabel(REPO, "area:x", "c5def5", "Area"),
        ],
    )
    steps = series(batch)
    assert [(s.kind, s.target) for s in steps] == [
        ("ensure_label", None),
        ("create_issue", New("t")),
        ("labels", E1),
        ("parent", E1),
        ("order", E1),
        ("close", E1),
        ("comment", E1),
        ("comment", New("t")),
    ]


def test_later_changes_win_within_one_issue() -> None:
    batch = ChangeBatch.of(
        {
            E1: [
                SetDimension("priority", "priority: low"),
                SetParent(E5),
                Close("completed"),
                SetDimension("priority", "priority: high"),
                SetParent(None),
                Close("not_planned"),
                AddArea("area:web"),
                RemoveArea("area:web"),
            ]
        }
    )
    labels, parent, close = series(batch)
    assert labels.changes == (SetDimension("priority", "priority: high"), RemoveArea("area:web"))
    assert parent.changes == (SetParent(None),)
    assert close.changes == (Close("not_planned"),)


def test_label_edit_adds_and_removes_only_what_the_changes_touch() -> None:
    edit = label_edit(
        [SetDimension("readiness", "design-needed"), AddArea("area:apple"), RemoveArea("area:web")],
        current=("agent-ready", "area:web", "someone-elses"),
        repo_labels=ON_GITHUB,
        schema=AMICUS,
    )
    # The alias that exists on GitHub is written (spec §6.2), in GitHub's spelling.
    assert edit.add == ("design needed", "apple")
    assert edit.remove == ("agent-ready", "area:web")


def test_label_edit_reports_every_missing_label() -> None:
    with pytest.raises(MissingLabelError) as raised:
        label_edit(
            [SetDimension("priority", "priority: medium"), AddArea("area:nope")],
            current=(),
            repo_labels=ON_GITHUB,
            schema=AMICUS,
        )
    assert raised.value.names == ("priority: medium", "area:nope")
    assert "never creates labels" in str(raised.value)


def test_label_edit_refuses_values_outside_the_schema() -> None:
    with pytest.raises(InvalidChangeError):
        label_edit([SetDimension("priority", "urgent")], (), ON_GITHUB, AMICUS)
    with pytest.raises(InvalidChangeError):
        label_edit([AddArea("web")], (), ON_GITHUB, AMICUS)


def test_a_batch_must_create_every_temp_id_it_names() -> None:
    with pytest.raises(InvalidChangeError):
        ChangeBatch.of({E1: [SetParent(New("ghost"))]})
    with pytest.raises(InvalidChangeError):
        ChangeBatch.of({New("ghost"): [Comment("x")]})
    with pytest.raises(InvalidChangeError):
        ChangeBatch.of(
            {},
            repo=[CreateIssue("t", REPO, "A", "", ()), CreateIssue("t", REPO, "B", "", ())],
        )
    with pytest.raises(InvalidChangeError):
        ChangeBatch.of({E1: [SetParent(E1)]})
    with pytest.raises(InvalidChangeError):
        ChangeBatch.of({E1: [SetParent(Existing("other/web", 3))]})


def test_changes_and_batches_round_trip_through_json() -> None:
    batch = ChangeBatch.of(
        {
            E1: [
                SetDimension("type", "bug"),
                AddArea("area:web"),
                RemoveArea("apple"),
                SetParent(New("t")),
                ReorderChild(E2, after=None),
                Close("duplicate", 1002, E2, "Duplicate of acme/web#2"),
                Comment("hi"),
            ]
        },
        repo=[
            EnsureLabel(REPO, "area:x", "c5def5", "Area", SchemaEntry("area", "area:x", ("x",))),
            CreateIssue("t", REPO, "T", "body", ("bug",)),
        ],
    )
    assert parse_batch(batch_json(batch)) == batch
    for _, changes in batch.issues:
        for change in changes:
            assert parse_change(change_json(change)) == change
    with pytest.raises(InvalidChangeError):
        parse_change({"kind": "teleport"})


def test_results_round_trip_and_group_per_issue() -> None:
    batch = ChangeBatch.of(
        {E1: [Comment("a")], New("t"): [Comment("b")]},
        repo=[CreateIssue("t", REPO, "T", "", ())],
    )
    create, first, second = series(batch)
    reset = datetime(2026, 10, 1, 13, tzinfo=UTC)
    result = BatchResult(
        (
            StepResult(create, "done"),
            StepResult(first, "failed", "boom"),
            StepResult(second, "skipped", "later"),
        ),
        {"t": CreatedIssue(REPO, 3, 1003)},
        stopped="limit",
        is_rate_limited=True,
        rate_limited_until=reset,
    )
    assert parse_result(batch, result_json(result)) == result
    assert not result.complete
    assert [s.state for s in result.per_issue[E1].steps] == ["failed"]
    assert [s.step.kind for s in result.repo_steps] == ["create_issue"]


def test_overlay_entries_join_the_schema_once() -> None:
    entry = SchemaEntry("area", "area:sync", ("sync",))
    merged = with_overlay(DEFAULT_SCHEMA, [entry, entry])
    assert merged.areas == (Canonical("area:sync", ("sync",)),)
    assert with_overlay(merged, [entry]) == merged
    readiness = with_overlay(AMICUS, [SchemaEntry("readiness", "Agent-Ready")])
    assert readiness.readiness == AMICUS.readiness  # already in the schema by label_key


NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _suggestion(kind: str, payload: dict[str, object], *, repo: str | None = REPO) -> Suggestion:
    return Suggestion(
        "s1",
        repo,
        None if repo is None else 1,
        None if repo is not None else "queue:mine",
        parse_kind(kind),
        payload,
        "why",
        "draft",
        NOW,
        NOW,
    )


@pytest.mark.parametrize(
    "make",
    [
        lambda: Existing("not a repo", 1),
        lambda: Existing(REPO, 0),
        lambda: New("bad id!"),
        lambda: SetDimension("area", "x"),  # type: ignore[arg-type]
        lambda: SetDimension("priority", " padded"),
        lambda: AddArea(""),
        lambda: RemoveArea("a" * 51),
        lambda: Close("completed", 5, None),
        lambda: Close("completed", 5, E2),
        lambda: Close("duplicate", 5, None),
        lambda: Close("duplicate", None, E2),
        lambda: Close("duplicate"),
        lambda: ChangeBatch.of({E1: [Close("duplicate", 1001, E1)]}),
        lambda: SetParent(E5, expected_parent=E2),
        lambda: Close("not_planned", comment="  "),
        lambda: Comment(""),
        lambda: EnsureLabel(REPO, "area:x", "red", "Area"),
        lambda: EnsureLabel(REPO, "area:x", "c5def5", "Area", SchemaEntry("area", "area:y")),
        lambda: CreateIssue("t", REPO, "", "", ()),
        lambda: ChangeBatch(((E1, ()), (Existing("ACME/web", 1), ()))),
        lambda: ChangeBatch.of({E1: [ReorderChild(E1)]}),
        lambda: ChangeBatch.of({E5: [ReorderChild(E1, after=E1)]}),
    ],
)
def test_invalid_changes_are_refused_when_made(make: Callable[[], object]) -> None:
    with pytest.raises(InvalidChangeError):
        make()


def test_targets_describe_themselves() -> None:
    assert str(E1) == "acme/web#1"
    assert str(New("t")) == "the new issue 't'"


def test_labels_needed_are_the_values_and_their_schema_names() -> None:
    needed = labels_needed(
        [AddArea("apple"), SetDimension("priority", "urgent"), Comment("x")], AMICUS
    )
    assert needed == {"apple", "area:apple", "platform:ios", "urgent"}


def test_check_change_refuses_values_the_schema_cant_place() -> None:
    check_change(Comment("fine"), AMICUS)
    check_change(RemoveArea("area:web"), AMICUS)
    with pytest.raises(InvalidChangeError):
        check_change(SetDimension("type", "chore"), AMICUS)
    with pytest.raises(InvalidChangeError):
        check_change(AddArea("area:"), AMICUS)


def test_approval_batches_by_kind() -> None:
    label = approval_batch(_suggestion("label", {"dimension": "area", "value": "area:web"}))
    assert label == ChangeBatch.of({E1: [AddArea("area:web")]})
    gap = approval_batch(_suggestion("readiness_gap", {"ready": False, "missing": ["x"]}))
    assert gap == ChangeBatch()
    dup = _suggestion("duplicate", {"repo": REPO, "n": 2, "similarity": 0.5})
    with pytest.raises(InvalidChangeError):
        approval_batch(dup)
    assert approval_batch(dup, 1002) == ChangeBatch.of(
        {E1: [Close("duplicate", 1002, E2, "Duplicate of acme/web#2", only_if_open=True)]}
    )
    with pytest.raises(InvalidChangeError):
        approval_batch(_suggestion("priority", {}))
    with pytest.raises(InvalidChangeError):
        approval_batch(_suggestion("group", {}, repo=None))


def test_pending_batches_group_per_issue_in_staged_order() -> None:
    staged = [
        PendingChange("a", E1, Comment("1"), NOW, "drag"),
        PendingChange("b", E2, Comment("2"), NOW, "menu"),
        PendingChange("c", E1, Comment("3"), NOW, "bulk"),
    ]
    assert pending_batch(staged) == ChangeBatch.of(
        {E1: [Comment("1"), Comment("3")], E2: [Comment("2")]}
    )
    assert parse_source("queue") == "queue"
    with pytest.raises(InvalidChangeError):
        parse_source("telepathy")


CHANGE = {"kind": "set_dimension", "dimension": "priority", "value": "priority: high"}


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "set_dimension", "dimension": "area", "value": "x"},
        {"kind": "set_dimension", "dimension": "priority", "value": 3},
        {"kind": "set_dimension"},
        {"kind": "set_parent", "parent": "acme/web#1"},
        {"kind": "set_parent", "parent": {"repo": REPO}},
        {"kind": "set_parent", "parent": None, "expects_parent": "yes"},
        {
            "kind": "set_parent",
            "parent": None,
            "expects_parent": True,
            "expected_parent": {"temp_id": "t"},
        },
        {"kind": "reorder_child", "child": {"repo": REPO, "n": True}, "after": None},
        {
            "kind": "close",
            "reason": "done",
            "duplicate_of_id": None,
            "duplicate_of_ref": None,
            "comment": None,
        },
        {
            "kind": "close",
            "reason": "duplicate",
            "duplicate_of_id": 3,
            "duplicate_of_ref": {"temp_id": "t"},
            "comment": None,
        },
        {
            "kind": "close",
            "reason": "duplicate",
            "duplicate_of_id": "3",
            "duplicate_of_ref": {"repo": REPO, "n": 2},
            "comment": None,
        },
        {
            "kind": "ensure_label",
            "repo": REPO,
            "name": "area:x",
            "color": "c5def5",
            "description": "",
            "schema_entry": "area",
        },
        {
            "kind": "ensure_label",
            "repo": REPO,
            "name": "area:x",
            "color": "c5def5",
            "description": "",
            "schema_entry": {"dimension": "hierarchy", "canonical": "area:x", "aliases": []},
        },
        {
            "kind": "ensure_label",
            "repo": REPO,
            "name": "area:x",
            "color": "c5def5",
            "description": "",
            "schema_entry": {"dimension": "area", "canonical": "area:x", "aliases": "x"},
        },
        {
            "kind": "create_issue",
            "temp_id": "t",
            "repo": REPO,
            "title": "T",
            "body": "",
            "labels": "bug",
        },
    ],
)
def test_malformed_change_json_is_refused(data: dict[str, object]) -> None:
    with pytest.raises(InvalidChangeError):
        parse_change(data)


def test_an_ensure_without_a_schema_entry_round_trips() -> None:
    plain = EnsureLabel(REPO, "theme:x", "c5def5", "Theme")
    assert parse_change(change_json(plain)) == plain


def test_issue_changes_refuse_repo_level_ones() -> None:
    assert parse_issue_change(CHANGE) == SetDimension("priority", "priority: high")
    with pytest.raises(InvalidChangeError):
        parse_issue_change(change_json(CreateIssue("t", REPO, "T", "", ())))
    with pytest.raises(InvalidChangeError):
        parse_batch({"issues": []})
    batch = ChangeBatch.of({E1: [Comment("a")]})
    with pytest.raises(InvalidChangeError):
        parse_result(batch, {"results": [], "created": {}})


def test_overlay_dimensions_merge_into_their_own_dimension() -> None:
    merged = with_overlay(
        DEFAULT_SCHEMA,
        [
            SchemaEntry("readiness", "waiting"),
            SchemaEntry("priority", "priority: urgent"),
            SchemaEntry("type", "chore"),
        ],
    )
    assert merged.readiness[-1] == Canonical("waiting")
    assert merged.priority[-1] == Canonical("priority: urgent")
    assert merged.type[-1] == Canonical("chore")


def test_an_approval_only_fills_a_dimension_github_left_missing() -> None:
    change = SetDimension("priority", "priority: high", only_if_missing=True)
    with pytest.raises(ChangedOnGitHubError) as raised:
        label_edit([change], ("priority: low",), ON_GITHUB, AMICUS)
    assert "changed on GitHub" in str(raised.value) and "priority: low" in str(raised.value)
    # Already the value it would set: nothing to do, no conflict.
    assert label_edit([change], ("priority: high",), ON_GITHUB, AMICUS).is_empty
    assert label_edit([change], (), ON_GITHUB, AMICUS).add == ("priority: high",)


def test_approval_batches_carry_their_fresh_read_preconditions() -> None:
    label = approval_batch(_suggestion("label", {"dimension": "type", "value": "bug"}))
    assert label == ChangeBatch.of({E1: [SetDimension("type", "bug", only_if_missing=True)]})
    parent = approval_batch(_suggestion("parent", {"repo": REPO, "n": 5}))
    assert parent == ChangeBatch.of({E1: [SetParent(E5, expects_parent=True)]})
    dup = approval_batch(_suggestion("duplicate", {"repo": REPO, "n": 2}), 1002)
    ((_, (close,)),) = dup.issues
    assert isinstance(close, Close) and close.only_if_open


def test_preconditions_and_conflicts_round_trip() -> None:
    changes: list[Change] = [
        SetDimension("type", "bug", only_if_missing=True),
        SetParent(None, expects_parent=True, expected_parent=E5),
        Close("completed", only_if_open=True),
    ]
    for change in changes:
        assert parse_change(change_json(change)) == change
    batch = ChangeBatch.of({E1: [Comment("a")]})
    (step,) = series(batch)
    result = BatchResult((StepResult(step, "failed", "moved", is_conflict=True),))
    assert parse_result(batch, result_json(result)) == result
    with pytest.raises(InvalidChangeError):
        parse_result(
            batch,
            {**result_json(result), "results": [{"state": "exploded", "cause": None}]},
        )


def test_a_close_comment_is_marked_as_the_closes() -> None:
    (_, comment) = series(ChangeBatch.of({E1: [Close("completed", comment="bye"), Comment("x")]}))
    assert comment.changes == (Comment("bye", from_close=True), Comment("x"))
