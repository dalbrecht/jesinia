# SPDX-License-Identifier: AGPL-3.0-or-later
"""Epic rules (spec §4, §6.2; plan Task 8; rulings IR17, IR27): what an epic is, what it may
contain, when it is ready, checklist conversion plans, proposals and delivery briefs. Pure."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jesinia.apps.issues.domain.changes import (
    CreateIssue,
    Existing,
    InvalidChangeError,
    New,
    SetParent,
    series,
)
from jesinia.apps.issues.domain.epics import (
    BRIEF_MARKER,
    CROSS_REPO_WARNING,
    Blocker,
    BriefLine,
    BriefReply,
    ChecklistItem,
    ContainmentRefusedError,
    Facts,
    GroupingReply,
    InvalidBriefError,
    Member,
    Node,
    Readiness,
    brief_comment,
    brief_scope,
    can_contain,
    checklist,
    compose_brief,
    default_scope,
    derived_acceptance,
    epic_readiness,
    group_drafts,
    groups_scope,
    is_brief_comment,
    is_cross_repo,
    is_epic,
    is_initiative,
    issue_readiness,
    lacks_epic_label,
    new_epic_plan,
    plan_conversion,
    proposal_batch,
    rolled_up_readiness,
    split_draft,
    split_scope,
    strip_marker,
)
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA
from jesinia.apps.issues.domain.suggestions import Suggestion, SuggestionStaleError, parse_kind
from jesinia.apps.issues.domain.triage import Triage, triage
from jesinia.services.github.domain.issue import Issue, IssueRef

REPO = "acme/web"
API = "acme/api"
ELSEWHERE = "other/web"
READY = ("agent-ready", "priority: high", "bug", "area:web")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def issue(n: int, labels: tuple[str, ...] = (), *, sub_issue_count: int = 0) -> Issue:
    return Issue(
        repo=REPO,
        number=n,
        id=1000 + n,
        title=f"Issue {n}",
        body="",
        state="open",
        state_reason=None,
        labels=labels,
        milestone=None,
        parent=None,
        sub_issues=(),
        created_at=T0,
        updated_at=T0,
        url=f"https://github.com/{REPO}/issues/{n}",
        author="dana",
        sub_issue_count=sub_issue_count,
    )


def member(
    n: int, labels: tuple[str, ...] = READY, *, repo: str = REPO, is_open: bool = True
) -> Member:
    return Member(repo, n, f"Issue {n}", is_open, triage(labels, 0, DEFAULT_SCHEMA))


def facts(
    n: int,
    *,
    repo: str = REPO,
    is_open: bool = True,
    parent: tuple[str, int] | None = None,
    is_initiative: bool = False,
) -> Facts:
    return Facts(repo, n, is_open, parent, is_initiative)


# --- what an epic is ---------------------------------------------------------------------------


def test_an_epic_has_the_label_or_sub_issues_and_an_initiative_its_own_label() -> None:
    assert is_epic(issue(1, ("epic",)), DEFAULT_SCHEMA)
    assert is_epic(issue(2, sub_issue_count=3), DEFAULT_SCHEMA)
    assert is_epic(replace(issue(3), sub_issues=(IssueRef(REPO, 4),)), DEFAULT_SCHEMA)
    assert not is_epic(issue(5, ("bug",)), DEFAULT_SCHEMA)
    assert is_initiative(issue(6, ("initiative",)), DEFAULT_SCHEMA)
    assert not is_initiative(issue(7, ("epic",)), DEFAULT_SCHEMA)


def test_children_without_the_label_suggest_adding_it() -> None:
    assert lacks_epic_label(issue(1, sub_issue_count=2), DEFAULT_SCHEMA)
    assert not lacks_epic_label(issue(2, ("epic",), sub_issue_count=2), DEFAULT_SCHEMA)
    assert not lacks_epic_label(issue(3, ("initiative",), sub_issue_count=2), DEFAULT_SCHEMA)
    assert not lacks_epic_label(issue(4), DEFAULT_SCHEMA)


# --- containment (Review Focus 3, IR17) --------------------------------------------------------


def test_epic_cannot_contain_itself_or_its_ancestor() -> None:
    epic, child, grandparent = Node(REPO, 1), Node(REPO, 2), Node(REPO, 3)
    refused = can_contain(epic, Node("ACME/Web", 1), ())
    assert refused is not None and refused.code == "self"
    # 2 sits under 1, which sits under 3: putting 3 (an ancestor) into 2 makes a cycle.
    cycle = can_contain(child, grandparent, (epic.key, grandparent.key))
    assert cycle is not None and cycle.code == "cycle" and "cycle" in cycle.reason
    assert can_contain(epic, child, (grandparent.key,)) is None


def test_an_initiative_never_goes_into_an_epic() -> None:
    refused = can_contain(Node(REPO, 1), Node(REPO, 2, is_initiative=True), ())
    assert refused is not None and refused.code == "initiative"
    assert can_contain(Node(REPO, 1, is_initiative=True), Node(REPO, 2), ()) is None
    nested = can_contain(Node(REPO, 1, is_initiative=True), Node(REPO, 2, is_initiative=True), ())
    assert nested is None


def test_cross_repo_children_warn_and_cross_owner_children_are_refused() -> None:
    epic = Node(REPO, 1)
    assert can_contain(epic, Node(API, 2), ()) is None
    assert is_cross_repo(epic, Node(API, 2)) and not is_cross_repo(epic, Node("ACME/WEB", 3))
    refused = can_contain(epic, Node(ELSEWHERE, 2), ())
    assert refused is not None and refused.code == "cross_owner"
    assert "same owner" in refused.reason
    error = ContainmentRefusedError(refused)
    assert isinstance(error, InvalidChangeError) and error.refusal == refused
    readiness = epic_readiness(member(1, ("epic", "priority: high")), [member(2, repo=API)])
    assert readiness.cross_repo and readiness.warning == CROSS_REPO_WARNING


# --- readiness ---------------------------------------------------------------------------------


def test_epic_ready_only_when_direct_children_ready() -> None:
    epic = member(1, ("epic", "priority: high"))
    # A direct child that is complete and agent-ready is ready, whatever its own children are.
    ready = epic_readiness(epic, [member(2), member(3, is_open=False, labels=("bug",))])
    assert ready == Readiness(True, (), False)
    blocked = epic_readiness(
        epic,
        [
            member(2),
            member(3, ("human-needed", "priority: high", "bug", "area:web")),
            member(4, ("bug",)),
            member(5, ("agent-ready", "human-needed", "priority: high", "bug", "area:web")),
            Member(REPO, 6, "Issue 6", True, None),
        ],
    )
    assert not blocked.ready
    assert [(b.n, b.reason) for b in blocked.blockers] == [
        (3, "readiness is human-needed, not agent-ready"),
        (4, "missing readiness, priority, area"),
        (5, "conflicting labels: readiness: agent-ready, human-needed"),
        (6, "not in the cache; sync its repo first"),
    ]
    assert blocked.blockers[0] == Blocker(REPO, 3, "readiness is human-needed, not agent-ready")


def test_an_epic_without_open_or_known_children_is_not_ready_and_says_why() -> None:
    epic = member(1, ("epic", "priority: high"))
    assert epic_readiness(epic, None).note == "its children haven't been fetched yet; refresh it"
    assert epic_readiness(epic, []).note == "it has no children yet"
    done = epic_readiness(epic, [member(2, is_open=False)])
    assert not done.ready and done.note == "every child is closed"
    assert epic_readiness(epic, None).warning is None


def test_an_issue_and_a_milestone_roll_up_their_own_readiness() -> None:
    assert issue_readiness(member(1)).ready
    unset = issue_readiness(member(2, ("priority: high", "bug", "area:web")))
    assert [b.reason for b in unset.blockers] == ["missing readiness"]
    closed = issue_readiness(member(3, is_open=False))
    assert not closed.ready and closed.note == "it is closed"
    milestone = rolled_up_readiness([member(4), member(5, ("bug",)), member(6, is_open=False)])
    assert not milestone.ready and [b.n for b in milestone.blockers] == [5]
    assert rolled_up_readiness([member(4)]).ready
    assert rolled_up_readiness([]).note == "it has no open issues"


def test_an_unset_readiness_on_a_complete_epic_child_blocks() -> None:
    child_epic = Member(
        REPO, 2, "Epic 2", True, triage(("epic", "priority: high"), 1, DEFAULT_SCHEMA)
    )
    blocked = epic_readiness(member(1, ("epic", "priority: high")), [child_epic])
    assert [b.reason for b in blocked.blockers] == ["readiness is unset, not agent-ready"]


# --- checklists --------------------------------------------------------------------------------


BODY = """Scope of the epic.

- [ ] Fix login #12
- [x] Ship [the API](https://example.com) acme/api#7
* [ ] https://github.com/acme/web/issues/14 needs work
- [ ] Write the docs
- [x] Already done, no link
  - [ ]
```
- [ ] not a task, in a code block
```
- [ ] other/web#3 from elsewhere
"""


def test_a_checklist_reads_tasks_and_their_links_outside_code_blocks() -> None:
    items = checklist(BODY, REPO)
    assert [(i.text, i.checked, i.ref) for i in items] == [
        ("Fix login #12", False, (REPO, 12)),
        ("Ship the API acme/api#7", True, (API, 7)),
        ("https://github.com/acme/web/issues/14 needs work", False, (REPO, 14)),
        ("Write the docs", False, None),
        ("Already done, no link", True, None),
        ("other/web#3 from elsewhere", False, (ELSEWHERE, 3)),
    ]
    assert checklist("no tasks here", REPO) == []
    assert checklist("~~~\n- [ ] fenced\n~~~\n- [ ] kept", REPO)[0].text == "kept"


def test_checklist_conversion_matches_links_and_drafts_new() -> None:
    items = checklist(BODY, REPO)
    known = {
        (REPO.casefold(), 12): facts(12),
        (API.casefold(), 7): facts(7, repo=API, is_open=False),  # checked off: a done child
        (REPO.casefold(), 14): facts(14, parent=(REPO, 99)),
    }
    plan = plan_conversion(facts(1), (), items, known, set())
    assert plan.epic == IssueRef(REPO, 1)
    assert [(c.target, c.is_new, c.warning) for c in plan.children] == [
        (Existing(REPO, 12), False, None),
        (Existing(API, 7), False, CROSS_REPO_WARNING),
        (New("item-4"), True, None),
    ]
    assert plan.children[2].title == "Write the docs"
    assert [(s.item, s.reason) for s in plan.skipped] == [
        ("https://github.com/acme/web/issues/14 needs work", f"already under {REPO}#99"),
        ("Already done, no link", "checked off with no linked issue: nothing to create"),
        (
            "other/web#3 from elsewhere",
            "another owner's issue (GitHub's sub-issues require the same owner)",
        ),
    ]
    # One approved batch: create the new issue, then link every child in checklist order.
    (create,) = plan.changes.repo
    assert isinstance(create, CreateIssue) and create.repo == REPO and create.labels == ()
    assert create.temp_id == "item-4" and f"{REPO}#1" in create.body
    links = [(t, c) for t, c in plan.changes.issues]
    assert [t for t, _ in links] == [Existing(REPO, 12), Existing(API, 7), New("item-4")]
    assert all(
        c == (SetParent(Existing(REPO, 1), expects_parent=True, expected_parent=None),)
        for _, c in links
    )
    assert [s.kind for s in series(plan.changes)][:2] == ["create_issue", "parent"]
    assert (
        len(plan.digest) == 64
        and plan.digest == plan_conversion(facts(1), (), items, known, set()).digest
    )


def test_conversion_skips_what_it_cant_or_needn_t_link() -> None:
    items = [
        ChecklistItem("cached nowhere #20", False, (REPO, 20)),
        ChecklistItem("already a child #21", False, (REPO, 21)),
        ChecklistItem("itself #1", False, (REPO, 1)),
        ChecklistItem("its parent #30", False, (REPO, 30)),
        ChecklistItem("an initiative #22", False, (REPO, 22)),
        ChecklistItem("twice #23", False, (REPO, 23)),
        ChecklistItem("twice again #23", False, (REPO, 23)),
        ChecklistItem("x" * 300, False, None),
    ]
    known = {(REPO.casefold(), n): facts(n, is_initiative=n == 22) for n in (1, 21, 22, 23, 30)}
    plan = plan_conversion(
        facts(1), ((REPO.casefold(), 30),), items, known, {(REPO.casefold(), 21)}
    )
    assert [s.reason for s in plan.skipped] == [
        "not in the cache; sync its repo first",
        "already a sub-issue",
        "an issue can't contain itself",
        "that would make a cycle: it is already above this epic",
        "an initiative sits above epics; it can't go inside one",
        "listed twice",
    ]
    assert [c.target for c in plan.children] == [Existing(REPO, 23), New("item-8")]
    assert len(plan.children[1].title) == 256


def test_a_long_checklist_converts_its_first_hundred_items() -> None:
    body = "\n".join(f"- [ ] task {i}" for i in range(105))
    plan = plan_conversion(facts(1), (), checklist(body, REPO), {}, set())
    assert len(plan.children) == 100
    assert plan.skipped[-1].reason == "over the 100-item limit"


def test_a_new_epic_plan_creates_the_epic_and_links_its_members() -> None:
    plan = new_epic_plan(REPO, "Sync reliability", "The scope.", [facts(2), facts(3, repo=API)])
    assert isinstance(plan.epic, CreateIssue) and plan.epic.temp_id == "epic"
    assert [c.target for c in plan.children] == [Existing(REPO, 2), Existing(API, 3)]
    assert plan.children[1].warning == CROSS_REPO_WARNING
    assert plan.changes.issues[0][1] == (
        SetParent(New("epic"), expects_parent=True, expected_parent=None),
    )
    refused = new_epic_plan(REPO, "T", "", [facts(4, repo=ELSEWHERE), facts(5, is_initiative=True)])
    assert refused.children == () and len(refused.skipped) == 2


# --- proposals ---------------------------------------------------------------------------------


def grouping(
    groups: list[dict[str, object]] | None = None,
    additions: list[dict[str, object]] | None = None,
    splits: list[dict[str, object]] | None = None,
) -> GroupingReply:
    return GroupingReply.from_json(
        {"groups": groups or [], "additions": additions or [], "splits": splits or []}
    )


def ref(n: int, repo: str = REPO) -> dict[str, object]:
    return {"repo": repo, "number": n}


def test_group_drafts_keep_only_known_ungrouped_issues_of_one_owner() -> None:
    candidates = {(REPO.casefold(), n): facts(n) for n in (2, 3, 4)}
    candidates[(API.casefold(), 5)] = facts(5, repo=API)
    candidates[(ELSEWHERE.casefold(), 6)] = facts(6, repo=ELSEWHERE)
    epics = {(REPO.casefold(), 1): facts(1), (REPO.casefold(), 8): facts(8)}
    reply = grouping(
        groups=[
            {"title": " Sync ", "scope": "Make sync\nreliable", "issues": [ref(2), ref(5, API),
             ref(6, ELSEWHERE), ref(2), ref(77)], "reason": "related"},
            {"title": "Too small", "scope": "s", "issues": [ref(3), ref(99)], "reason": "r"},
            {"title": "Blank reason", "scope": "s", "issues": [ref(3), ref(4)], "reason": "  "},
        ],
        additions=[
            {"epic": ref(1), "issue": ref(4), "reason": "fits"},
            {"epic": ref(42), "issue": ref(4), "reason": "unknown epic"},
            {"epic": ref(1), "issue": ref(6, ELSEWHERE), "reason": "other owner"},
            {"epic": ref(8), "issue": ref(8), "reason": "itself"},
        ],
    )  # fmt: skip
    drafts = group_drafts(reply, candidates, epics)
    assert [(d.kind, d.reason) for d in drafts] == [("group", "related"), ("group", "fits")]
    assert drafts[0].payload == {
        "repo": REPO,
        "title": "Sync",
        "scope": "Make sync\nreliable",
        "issues": [{"repo": REPO, "n": 2}, {"repo": API, "n": 5}],
        "cross_repo": True,
    }
    assert drafts[1].payload == {"epic": {"repo": REPO, "n": 1}, "issues": [{"repo": REPO, "n": 4}]}
    assert groups_scope(None) == "groups:*" and groups_scope("Acme/Web") == "groups:acme/web"


def test_a_split_draft_is_about_the_asked_issue_only() -> None:
    reply = grouping(
        splits=[
            {"issue": ref(9), "parts": [{"title": "a", "body": ""}] * 2, "reason": "other"},
            {
                "issue": ref(2),
                "parts": [{"title": " Part one ", "body": "b1"}, {"title": "Part two", "body": ""}],
                "reason": "too big\nfor one PR",
            },
        ]
    )
    draft = split_draft(reply, facts(2))
    assert draft is not None and draft.kind == "split" and draft.reason == "too big for one PR"
    assert draft.payload == {
        "issue": {"repo": REPO, "n": 2},
        "parts": [{"title": "Part one", "body": "b1"}, {"title": "Part two", "body": ""}],
    }
    assert split_draft(reply, facts(3)) is None
    blank = grouping(
        splits=[{"issue": ref(2), "parts": [{"title": "a", "body": ""}] * 2, "reason": " "}]
    )
    assert split_draft(blank, facts(2)) is None
    assert split_scope("Acme/Web", 2) == "split:acme/web#2"


def suggestion(kind: str, payload: dict[str, object]) -> Suggestion:
    return Suggestion(
        "s1",
        None,
        None,
        "groups:*",
        parse_kind(kind),
        payload,
        "r",
        "draft",
        T0,
        T0,
    )


def test_approving_a_group_creates_the_epic_and_links_its_issues() -> None:
    payload: dict[str, object] = {
        "repo": REPO,
        "title": "Sync",
        "scope": "Make it reliable",
        "issues": [{"repo": REPO, "n": 2}, {"repo": REPO, "n": 3}],
        "cross_repo": False,
    }
    known = {(REPO.casefold(), n): facts(n) for n in (2, 3)}
    batch = proposal_batch(suggestion("group", payload), known, {})
    (create,) = batch.repo
    assert isinstance(create, CreateIssue) and create.title == "Sync"
    assert create.body.startswith("Make it reliable")
    assert [t for t, _ in batch.issues] == [Existing(REPO, 2), Existing(REPO, 3)]
    gone = dict(known)
    gone[(REPO.casefold(), 3)] = facts(3, parent=(REPO, 50))
    with pytest.raises(SuggestionStaleError, match="already under"):
        proposal_batch(suggestion("group", payload), gone, {})
    with pytest.raises(SuggestionStaleError, match="not in the cache"):
        proposal_batch(suggestion("group", payload), {}, {})
    closed = {**known, (REPO.casefold(), 2): facts(2, is_open=False)}
    with pytest.raises(SuggestionStaleError, match="closed"):
        proposal_batch(suggestion("group", payload), closed, {})


def test_approving_an_addition_links_the_issue_under_the_epic() -> None:
    payload: dict[str, object] = {
        "epic": {"repo": REPO, "n": 1},
        "issues": [{"repo": REPO, "n": 4}],
    }
    known = {(REPO.casefold(), 1): facts(1), (REPO.casefold(), 4): facts(4)}
    batch = proposal_batch(suggestion("group", payload), known, {})
    assert batch.repo == () and batch.issues == (
        (Existing(REPO, 4), (SetParent(Existing(REPO, 1), expects_parent=True),)),
    )
    # The issue became an ancestor of the epic since: refused with the reason.
    with pytest.raises(SuggestionStaleError, match="cycle"):
        proposal_batch(
            suggestion("group", payload), known, {(REPO.casefold(), 1): ((REPO.casefold(), 4),)}
        )
    closed = {**known, (REPO.casefold(), 1): facts(1, is_open=False)}
    with pytest.raises(SuggestionStaleError, match="closed"):
        proposal_batch(suggestion("group", payload), closed, {})


def test_approving_a_split_creates_the_parts_under_the_issue() -> None:
    payload: dict[str, object] = {
        "issue": {"repo": REPO, "n": 2},
        "parts": [{"title": "One", "body": ""}, {"title": "Two", "body": "b"}],
    }
    batch = proposal_batch(suggestion("split", payload), {(REPO.casefold(), 2): facts(2)}, {})
    assert [c.temp_id for c in batch.repo if isinstance(c, CreateIssue)] == ["part-1", "part-2"]
    assert [t for t, _ in batch.issues] == [New("part-1"), New("part-2")]
    assert batch.issues[0][1] == (SetParent(Existing(REPO, 2), expects_parent=True),)
    assert "Part of acme/web#2" in next(c for c in batch.repo if isinstance(c, CreateIssue)).body
    with pytest.raises(SuggestionStaleError, match="closed"):
        proposal_batch(
            suggestion("split", payload), {(REPO.casefold(), 2): facts(2, is_open=False)}, {}
        )
    with pytest.raises(InvalidChangeError):
        proposal_batch(suggestion("brief", {}), {}, {})
    with pytest.raises(InvalidChangeError, match="malformed"):
        proposal_batch(suggestion("group", {}), {}, {})


# --- briefs ------------------------------------------------------------------------------------


def lines() -> list[BriefLine]:
    return [
        BriefLine(
            REPO, 2, "Fix login", "https://github.com/acme/web/issues/2", True, "agent-ready"
        ),
        BriefLine(API, 3, "Ship API", "https://github.com/acme/api/issues/3", False, None),
    ]


def test_a_brief_holds_scope_children_in_order_criteria_and_links() -> None:
    text = compose_brief(
        "Epic: Login",
        REPO,
        1,
        "https://github.com/acme/web/issues/1",
        "Make login work.",
        lines(),
        ["acme/web#2: it logs in"],
        cross_repo=True,
    )
    assert BRIEF_MARKER not in text
    assert (
        text.index("## Scope")
        < text.index("## Children, in order")
        < text.index("## Acceptance criteria")
        < text.index("## Links")
    )
    assert "1. [ ] acme/web#2 Fix login (agent-ready)" in text
    assert "2. [x] acme/api#3 Ship API (closed)" in text
    assert "- acme/web#2: it logs in" in text
    assert CROSS_REPO_WARNING in text
    bare = compose_brief("T", REPO, 1, "u", "", [], [], cross_repo=False)
    assert "No children yet." in bare and "None drawn from the children yet." in bare


def test_a_default_scope_and_criteria_come_from_the_issues_text() -> None:
    body = "First para\nstill first.\n\n- [ ] a task\n\nSecond para."
    assert default_scope(body) == "First para still first.\n\nSecond para."
    assert default_scope("") == ""
    assert len(default_scope("x" * 5000)) == 1500
    child = (
        "Intro\n\n## Acceptance criteria\n- logs in\n* [ ] keeps the session\nnot a bullet\n"
        "## Next\n- no"
    )
    assert derived_acceptance([(REPO, 2, child), (REPO, 3, "nothing")]) == [
        "acme/web#2: logs in",
        "acme/web#2: keeps the session",
    ]


def test_a_brief_comment_carries_the_marker_once() -> None:
    comment = brief_comment(f"{BRIEF_MARKER}\nHello {BRIEF_MARKER} world")
    assert comment.startswith(f"{BRIEF_MARKER}\n") and comment.count(BRIEF_MARKER) == 1
    assert is_brief_comment(f"  {comment}") and not is_brief_comment("Hello")
    assert strip_marker(comment) == "Hello  world"
    with pytest.raises(InvalidBriefError):
        brief_comment(f"  {BRIEF_MARKER}  ")
    with pytest.raises(InvalidBriefError):
        brief_comment("x" * 70_000)
    assert brief_scope("Acme/Web", 1) == "brief:acme/web#1"


def test_a_brief_reply_reads_its_scope_and_criteria() -> None:
    reply = BriefReply.from_json({"scope": "S", "acceptance_criteria": ["a", " ", "b\nc"]})
    assert reply.scope == "S" and reply.acceptance_criteria == ("a", "b c")


def test_a_triage_without_cache_stays_a_triage_type() -> None:
    assert isinstance(member(1).triage, Triage)
