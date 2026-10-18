# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suggestion drafts (spec §3.1, §6.2 "New areas"; plan Task 5): what an agent's triage reply
may become, and how the author edits or dismisses a draft. Pure."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA, Canonical
from jesinia.apps.issues.domain.suggestions import (
    ISSUE_KINDS,
    REASON_MAX,
    SUGGESTION_KINDS,
    Candidate,
    Draft,
    InvalidSuggestionEditError,
    InvalidSuggestionFilterError,
    LabelProposal,
    ReadinessProposal,
    RefProposal,
    Suggestion,
    SuggestionDomainError,
    SuggestionStateError,
    TriageContext,
    TriageReply,
    dismiss,
    edit,
    fresh_drafts,
    identity,
    one_line,
    parse_kind,
    parse_state,
    stale,
    triage_drafts,
)
from jesinia.apps.issues.domain.triage import triage

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SCHEMA = replace(
    DEFAULT_SCHEMA,
    areas=(Canonical("area:web", ("wiki",)), Canonical("area:apple", ("platform:ios",))),
    priority=(Canonical("priority: high", ("p1",)), *DEFAULT_SCHEMA.priority[1:]),
)
REPO = "acme/web"


CANDIDATES = {
    ("acme/web", 3): Candidate("acme/web", 3, 0.61234),
    ("acme/api", 9): Candidate("acme/api", 9, 0.3),
}


def context(
    labels: tuple[str, ...] = (),
    *,
    candidates: dict[tuple[str, int], Candidate] | None = None,
    epics: dict[tuple[str, int], tuple[str, int]] | None = None,
    has_parent: bool = False,
) -> TriageContext:
    return TriageContext(
        repo=REPO,
        n=7,
        triage=triage(labels, 0, SCHEMA),
        schema=SCHEMA,
        candidates=CANDIDATES if candidates is None else candidates,
        epics={("acme/web", 40): ("acme/web", 40)} if epics is None else epics,
        has_parent=has_parent,
    )


def reply(
    labels: tuple[LabelProposal, ...] = (),
    duplicates: tuple[RefProposal, ...] = (),
    parent: RefProposal | None = None,
    ready: ReadinessProposal = ReadinessProposal(False, (), "unclear"),
) -> TriageReply:
    return TriageReply(labels, duplicates, parent, ready)


def suggestion(kind: str = "label", state: str = "draft", **payload: object) -> Suggestion:
    return Suggestion(
        id="s1",
        repo=REPO,
        n=7,
        scope=None,
        kind=parse_kind(kind),
        payload=payload or {"dimension": "priority", "value": "priority: high"},
        reason="because",
        state=parse_state(state),
        created_at=T0,
        updated_at=T0,
    )


def test_kinds_include_issue_kinds_and_the_later_draft_kinds() -> None:
    assert ISSUE_KINDS == ("label", "duplicate", "parent", "readiness_gap", "new_area")
    assert set(SUGGESTION_KINDS) >= {
        *ISSUE_KINDS, "priority", "group", "split", "queue_next", "queue_order", "queue_split",
        "brief",
    }  # fmt: skip
    with pytest.raises(InvalidSuggestionFilterError):
        parse_kind("close")
    with pytest.raises(InvalidSuggestionFilterError):
        parse_state("applied")
    assert issubclass(InvalidSuggestionFilterError, SuggestionDomainError)


def test_one_line_collapses_whitespace_and_caps() -> None:
    assert one_line("  a\n\tb  ") == "a b"
    assert len(one_line("x" * (REASON_MAX + 9))) == REASON_MAX


def test_reply_from_json_reads_a_validated_reply() -> None:
    parsed = TriageReply.from_json(
        {
            "labels": [{"dimension": "type", "value": "bug", "reason": "r"}],
            "duplicates": [{"repo": "acme/web", "number": 3, "reason": "same"}],
            "parent": {"repo": "acme/web", "number": 40, "reason": "fits"},
            "agent_ready": {"ready": True, "missing": ["tests"], "reason": "ok"},
        }
    )
    assert parsed.labels == (LabelProposal("type", "bug", "r"),)
    assert parsed.duplicates == (RefProposal("acme/web", 3, "same"),)
    assert parsed.parent == RefProposal("acme/web", 40, "fits")
    assert parsed.agent_ready == ReadinessProposal(True, ("tests",), "ok")
    no_parent = TriageReply.from_json(
        {
            "labels": [],
            "duplicates": [],
            "parent": None,
            "agent_ready": {"ready": False, "missing": [], "reason": "x"},
        }
    )
    assert no_parent.parent is None


def test_label_drafts_only_for_missing_dimensions_mapped_to_canonical_values() -> None:
    ctx = context(("bug", "wiki"))  # type and area present; readiness and priority missing
    drafts = triage_drafts(
        reply(
            labels=(
                LabelProposal("type", "enhancement", "present already"),
                LabelProposal("area", "area:apple", "present already"),
                LabelProposal("priority", "P1", " users\nblocked "),
                LabelProposal("priority", "priority: low", "second value for one dimension"),
                LabelProposal("readiness", "ship-it", "not a schema value"),
                LabelProposal("readiness", "human-needed", "   "),
            )
        ),
        ctx,
    )
    assert drafts == [
        Draft("label", {"dimension": "priority", "value": "priority: high"}, "users blocked")
    ]


def test_conflicted_dimension_gets_no_draft() -> None:
    ctx = context(("bug", "enhancement", "area:web"))
    assert "type" not in ctx.triage.missing
    proposals = (LabelProposal("type", "bug", "pick one"),)
    assert triage_drafts(reply(labels=proposals), ctx) == []


def test_areas_known_new_prefixed_and_refused() -> None:
    ctx = context(("bug",))
    drafts = triage_drafts(
        reply(
            labels=(
                LabelProposal("area", "platform:ios", "alias of a known area"),
                LabelProposal("area", "area:apple", "repeat"),
                LabelProposal("area", "billing", "new, written without the prefix"),
                LabelProposal("area", "area:a,b", "a comma can't be listed by"),
                LabelProposal("area", "area:" + "x" * 60, "too long"),
                LabelProposal("area", "area:", "empty"),
                LabelProposal("area", "area:bill\u202eing", "a bidi control"),
                LabelProposal("area", "area:zero\u200bwidth", "a zero-width space"),
            )
        ),
        ctx,
    )
    assert drafts == [
        Draft("label", {"dimension": "area", "value": "area:apple"}, "alias of a known area"),
        Draft(
            "new_area",
            {"dimension": "area", "value": "area:billing"},
            "new, written without the prefix",
        ),
    ]


def test_duplicates_must_be_prefiltered_candidates_and_never_the_issue_itself() -> None:
    ctx = context(("bug",), candidates={**CANDIDATES, (REPO, 7): Candidate(REPO, 7, 1.0)})
    drafts = triage_drafts(
        reply(
            duplicates=(
                RefProposal("ACME/web", 3, "same crash"),
                RefProposal("acme/web", 3, "repeat"),
                RefProposal("acme/web", 5, "not a candidate"),
                RefProposal("acme/web", 7, "itself"),
            )
        ),
        ctx,
    )
    assert drafts == [
        Draft("duplicate", {"repo": "acme/web", "n": 3, "similarity": 0.612}, "same crash")
    ]


def test_parent_must_be_a_known_epic_and_only_for_an_orphan_non_epic() -> None:
    proposal = RefProposal("acme/web", 40, "belongs to onboarding")
    assert triage_drafts(reply(parent=proposal), context(("bug",))) == [
        Draft("parent", {"repo": "acme/web", "n": 40}, "belongs to onboarding")
    ]
    assert triage_drafts(reply(parent=RefProposal("acme/web", 41, "x")), context(("bug",))) == []
    assert triage_drafts(reply(parent=proposal), context(("bug",), has_parent=True)) == []
    assert triage_drafts(reply(parent=proposal), context(("epic",))) == []
    assert (
        triage_drafts(
            reply(parent=RefProposal(REPO, 7, "x")), context(epics={(REPO, 7): (REPO, 7)})
        )
        == []
    )


def test_readiness_gap_for_agent_ready_that_is_not_ready() -> None:
    gaps = ReadinessProposal(False, ("acceptance criteria", " repro\nsteps "), "vague")
    labelled = triage_drafts(reply(ready=gaps), context(("agent-ready",)))
    assert labelled == [
        Draft(
            "readiness_gap",
            {"ready": False, "missing": ["acceptance criteria", "repro steps"]},
            "vague",
        )
    ]
    # Proposed as agent-ready by the same reply: the gap is still drafted.
    proposed = triage_drafts(
        reply(labels=(LabelProposal("readiness", "agent-ready", "small"),), ready=gaps), context()
    )
    assert [d.kind for d in proposed] == ["label", "readiness_gap"]
    # Ready, or nothing missing named: no gap.
    assert (
        triage_drafts(reply(ready=ReadinessProposal(False, (), "x")), context(("agent-ready",)))
        == []
    )
    assert (
        triage_drafts(reply(ready=ReadinessProposal(True, (), "x")), context(("agent-ready",)))
        == []
    )
    # Another readiness: nothing about agent-ready.
    assert triage_drafts(reply(ready=gaps), context(("blocked",))) == []


def test_human_needed_that_could_be_ready_is_drafted_as_ready() -> None:
    ready = ReadinessProposal(True, (), "well specified")
    assert triage_drafts(reply(ready=ready), context(("human-needed",))) == [
        Draft("readiness_gap", {"ready": True, "missing": []}, "well specified")
    ]
    assert triage_drafts(reply(ready=ready), context(("human-needed", "epic"))) == []


def test_identity_and_fresh_drafts_skip_what_the_author_already_handled() -> None:
    assert identity("label", {"dimension": "priority", "value": "priority: low"}) == (
        "label",
        "priority",
    )
    assert identity("new_area", {"dimension": "area", "value": "Area:X"}) == (
        "label",
        "area",
        "area:x",
    )
    assert identity("duplicate", {"repo": "ACME/web", "n": 3}) == ("duplicate", "acme/web", 3)
    assert identity("readiness_gap", {"ready": True, "missing": []}) == ("readiness_gap",)
    assert identity("brief", {"text": "x"}) == ("brief",)
    kept = [
        # Priority dismissed while the issue read "h1": no more priority drafts while it does.
        replace(suggestion(state="dismissed"), basis="h1"),
        suggestion("duplicate", "edited", repo="acme/web", n=3),
        # Approved advice never suppresses (ruling IR38a): what it set may be gone again.
        suggestion("label", "approved", dimension="type", value="bug"),
        suggestion("parent", "approved", repo="acme/web", n=40),
    ]
    drafts = [
        Draft("label", {"dimension": "priority", "value": "priority: low"}, "r"),
        Draft("label", {"dimension": "type", "value": "bug"}, "r"),
        Draft("duplicate", {"repo": "acme/web", "n": 3, "similarity": 0.5}, "r"),
        Draft("duplicate", {"repo": "acme/web", "n": 4, "similarity": 0.5}, "r"),
        Draft("parent", {"repo": "acme/web", "n": 40}, "r"),
    ]
    assert fresh_drafts(drafts, kept, "h1") == [drafts[1], drafts[3], drafts[4]]
    # Once the issue's title, body or labels change, the dismissal no longer holds.
    assert fresh_drafts(drafts, kept, "h2") == [drafts[0], drafts[1], drafts[3], drafts[4]]


def test_an_edited_area_keeps_the_identity_it_was_proposed_with() -> None:
    area = suggestion(dimension="area", value="area:web")
    edited = edit(area, {"dimension": "area", "value": "billing"}, None, SCHEMA, T0)
    assert edited.origin == ("label", "area", "area:web")
    again = edit(edited, {"dimension": "area", "value": "area:apple"}, None, SCHEMA, T0)
    assert again.origin == ("label", "area", "area:web")
    rerun = [
        Draft("label", {"dimension": "area", "value": "area:web"}, "r"),
        Draft("label", {"dimension": "area", "value": "area:apple"}, "r"),
        Draft("label", {"dimension": "area", "value": "area:docs"}, "r"),
    ]
    assert fresh_drafts(rerun, [again], None) == [rerun[2]]


def test_stale_drafts_lose_their_premise() -> None:
    open_issue = triage(("bug",), 0, SCHEMA)  # readiness, priority and area missing
    priority = suggestion()
    assert not stale(priority, open_issue, has_parent=False, is_open=True)
    filled = triage(("bug", "priority: low"), 0, SCHEMA)
    assert stale(priority, filled, has_parent=False, is_open=True)
    assert stale(priority, open_issue, has_parent=False, is_open=False)
    parent = suggestion("parent", repo="acme/web", n=40)
    assert not stale(parent, open_issue, has_parent=False, is_open=True)
    assert stale(parent, open_issue, has_parent=True, is_open=True)
    assert stale(parent, triage(("epic",), 0, SCHEMA), has_parent=False, is_open=True)
    duplicate = suggestion("duplicate", repo="acme/web", n=3)
    assert not stale(duplicate, filled, has_parent=True, is_open=True)
    could_be_ready = suggestion("readiness_gap", ready=True, missing=[])
    assert not stale(
        could_be_ready, triage(("human-needed",), 0, SCHEMA), has_parent=False, is_open=True
    )
    assert stale(
        could_be_ready, triage(("agent-ready",), 0, SCHEMA), has_parent=False, is_open=True
    )
    gap = suggestion("readiness_gap", ready=False, missing=["tests"])
    assert not stale(gap, open_issue, has_parent=False, is_open=True)
    assert not stale(gap, triage(("agent-ready",), 0, SCHEMA), has_parent=False, is_open=True)
    assert stale(gap, triage(("blocked",), 0, SCHEMA), has_parent=False, is_open=True)
    assert not stale(suggestion("brief", text="x"), open_issue, has_parent=False, is_open=False)
    edited = suggestion(state="edited")
    assert not stale(edited, filled, has_parent=False, is_open=False)  # the author's now


def test_edit_label_keeps_dimension_and_resolves_the_value() -> None:
    edited = edit(
        suggestion(), {"dimension": "priority", "value": "priority: low"}, None, SCHEMA, T0
    )
    assert edited.state == "edited" and edited.kind == "label"
    assert edited.payload == {"dimension": "priority", "value": "priority: low"}
    assert edited.reason == "because"
    again = edit(edited, {"dimension": "priority", "value": "p1"}, " mine\n", SCHEMA, T0)
    assert again.payload["value"] == "priority: high" and again.reason == "mine"
    for bad in (
        {"dimension": "type", "value": "bug"},  # another dimension
        {"dimension": "priority", "value": "urgent"},  # not a schema value
        {"dimension": "priority", "value": 3},
        {"dimension": "priority", "value": "p1", "extra": 1},
    ):
        with pytest.raises(InvalidSuggestionEditError):
            edit(suggestion(), bad, None, SCHEMA, T0)
    with pytest.raises(InvalidSuggestionEditError):
        edit(suggestion(), {"dimension": "priority", "value": "p1"}, "  ", SCHEMA, T0)


def test_edit_area_switches_between_label_and_new_area() -> None:
    area = suggestion(dimension="area", value="area:web")
    became_new = edit(area, {"dimension": "area", "value": "billing"}, None, SCHEMA, T0)
    assert became_new.kind == "new_area" and became_new.payload["value"] == "area:billing"
    back = edit(became_new, {"dimension": "area", "value": "wiki"}, None, SCHEMA, T0)
    assert back.kind == "label" and back.payload["value"] == "area:web"
    with pytest.raises(InvalidSuggestionEditError):
        edit(area, {"dimension": "area", "value": "a,b"}, None, SCHEMA, T0)


def test_edit_refs_readiness_and_later_kinds() -> None:
    dup = suggestion("duplicate", repo="acme/web", n=3, similarity=0.4)
    assert edit(dup, {"repo": "acme/api", "n": 12}, None, SCHEMA, T0).payload == {
        "repo": "acme/api",
        "n": 12,
    }
    parent = suggestion("parent", repo="acme/web", n=40)
    for bad in (
        {"repo": "acme/web", "n": 7},  # the issue itself
        {"repo": "ACME/web", "n": 7},
        {"repo": "other/web", "n": 40},  # GitHub's sub-issues need the same owner
        {"repo": "nope", "n": 1},
        {"repo": "a/b", "n": 0},
        {"repo": "a/b", "n": True},
        {"repo": "a/b"},
    ):
        with pytest.raises(InvalidSuggestionEditError):
            edit(parent, bad, None, SCHEMA, T0)
    gap = suggestion("readiness_gap", ready=False, missing=["tests"])
    assert edit(gap, {"ready": False, "missing": [" a\nb "]}, None, SCHEMA, T0).payload == {
        "ready": False,
        "missing": ["a b"],
    }
    for bad_gap in (
        {"ready": "no", "missing": []},
        {"ready": False, "missing": "tests"},
        {"ready": False, "missing": [1]},
        {"ready": False, "missing": ["x"] * 11},
        {"ready": False, "missing": ["  "]},
    ):
        with pytest.raises(InvalidSuggestionEditError):
            edit(gap, bad_gap, None, SCHEMA, T0)
    with pytest.raises(InvalidSuggestionEditError):
        edit(dup, {"repo": "acme/web", "n": 7}, None, SCHEMA, T0)
    with pytest.raises(InvalidSuggestionEditError, match="can't be edited yet"):
        edit(suggestion("brief", text="x"), {"text": "y"}, None, SCHEMA, T0)


def test_only_open_drafts_can_be_edited_and_dismiss_is_idempotent() -> None:
    dismissed = dismiss(suggestion(), T0, "h1")
    assert dismissed.state == "dismissed" and dismissed.basis == "h1"
    assert dismiss(dismissed, T0, "h2") is dismissed
    with pytest.raises(SuggestionStateError):
        edit(dismissed, {"dimension": "priority", "value": "p1"}, None, SCHEMA, T0)
    approved = suggestion(state="approved")
    with pytest.raises(SuggestionStateError):
        dismiss(approved, T0)
    with pytest.raises(SuggestionStateError):
        edit(approved, {"dimension": "priority", "value": "p1"}, None, SCHEMA, T0)
