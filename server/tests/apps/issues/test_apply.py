# SPDX-License-Identifier: AGPL-3.0-or-later
"""The apply engine and the pending-changes tray (spec §6.1, §6.2, §8; plan Task 6).

Each issue's changes run as one ordered series against a fresh read: labels as add/remove (never
a replace), then the parent link, the sub-issue order, the close and the comment. A series stops
at its first failure and Retry runs only what didn't apply. Labels are never created implicitly
(IR4): ``EnsureLabel`` goes through Task 4's executor (IR12) and every label is checked live
before it is added. A rate limit stops the batch (IR5).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jesinia.apps.issues.application.apply import ApplyEngine
from jesinia.apps.issues.application.labels import WRITE_INTERVAL
from jesinia.apps.issues.application.pending import ApplyService, PendingService
from jesinia.apps.issues.application.suggest import SuggestService
from jesinia.apps.issues.application.sync import LABELS_YML, SyncService
from jesinia.apps.issues.domain.changes import (
    AddArea,
    ChangeBatch,
    Close,
    Comment,
    CreateIssue,
    EnsureLabel,
    Existing,
    New,
    NothingPendingError,
    RemoveArea,
    ReorderChild,
    SetDimension,
    SetParent,
)
from jesinia.apps.issues.domain.schema import SchemaEntry
from jesinia.apps.issues.domain.suggestions import (
    DuplicateNotCachedError,
    Suggestion,
    SuggestionFilters,
    SuggestionStaleError,
    SuggestionStateError,
    parse_kind,
    parse_state,
)
from jesinia.apps.issues.infrastructure.apply_store import SqliteApplyStore
from jesinia.apps.issues.infrastructure.store import SqliteIssuesStore
from jesinia.apps.issues.infrastructure.suggestion_store import SqliteSuggestionStore
from jesinia.apps.issues.module import ISSUES_DB
from jesinia.core.db import open_db
from jesinia.services.github.application.service import (
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.issue import CloseReason, CommentRef, Issue
from tests.apps.issues.test_labels import AMICUS_YML, BackOff, FakeGitHub, Pauses
from tests.apps.issues.test_sync import REPO, FakeClock, issue

RESET = datetime(2026, 10, 1, 13, 0).astimezone()
LABELS = (
    "agent-ready",
    "human-needed",
    "design needed",
    "bug",
    "enhancement",
    "priority: high",
    "priority: medium",
    "area:web",
    "area:api",
)


class FakeWrites(FakeGitHub):
    """``FakeGitHub`` plus the issue writes the apply engine makes: single reads, sub-issue
    links, close, comment and create, applied to the in-memory repo."""

    def __init__(self) -> None:
        super().__init__()
        self.parents: dict[tuple[str, int], tuple[str, int]] = {}
        self.comments_posted: list[tuple[int, str]] = []

    async def get(self, repo: str, n: int) -> Issue:
        self.calls.append(("get", (repo, n)))
        error = self.fail.get(f"get:{n}")
        if error is not None:
            raise error
        found = self.issues.get((repo, n))
        if found is None:
            raise GitHubRequestError(404, "Not Found")
        return found

    async def parent(self, repo: str, n: int) -> Issue | None:
        self.calls.append(("parent", (repo, n)))
        link = self.parents.get((repo, n))
        return None if link is None else self.issues[link]

    async def add_sub_issue(
        self, repo: str, n: int, child_id: int, *, replace_parent: bool = False
    ) -> None:
        self._write("add_sub_issue", n, n, child_id, replace_parent)
        child = next(i for i in self.issues.values() if i.id == child_id)
        self.parents[(child.repo, child.number)] = (repo, n)

    async def remove_sub_issue(self, repo: str, n: int, child_id: int) -> None:
        self._write("remove_sub_issue", n, n, child_id)
        child = next(i for i in self.issues.values() if i.id == child_id)
        self.parents.pop((child.repo, child.number), None)

    async def reprioritize_sub_issue(
        self, repo: str, n: int, child_id: int, after_id: int | None
    ) -> None:
        self._write("reprioritize_sub_issue", n, n, child_id, after_id)

    async def close(
        self, repo: str, n: int, state_reason: CloseReason, duplicate_of: int | None = None
    ) -> Issue:
        self._write("close", n, n, state_reason, duplicate_of)
        closed = replace(self.issues[(repo, n)], state="closed", state_reason=state_reason)
        self.issues[(repo, n)] = closed
        return closed

    async def comment(self, repo: str, n: int, body: str) -> CommentRef:
        self._write("comment", n, n, body)
        self.comments_posted.append((n, body))
        return CommentRef(1, f"https://github.com/{repo}/issues/{n}#c1")

    async def create_issue(self, repo: str, title: str, body: str, labels: Sequence[str]) -> Issue:
        self._write("create_issue", title, title, tuple(labels))
        n = max(number for (_, number) in self.issues) + 1
        created = replace(issue(n, labels=tuple(labels)), title=title, body=body)
        self.put(created)
        return created


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, ISSUES_DB)
    yield connection
    connection.close()


class World:
    """The services over one ``issues.sqlite`` and one fake GitHub."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.github = FakeWrites()
        self.github.files[(REPO, LABELS_YML)] = AMICUS_YML
        self.github.set_labels(*LABELS)
        self.clock = FakeClock()
        self.store = SqliteIssuesStore(conn)
        self.drafts = SqliteSuggestionStore(conn)
        self.applied = SqliteApplyStore(conn)
        self.pauses = Pauses()
        self.back_off = BackOff()
        self.suggest = SuggestService(
            self.drafts, self.store, lambda: None, lambda: None, self.clock
        )
        self.sync = SyncService(
            self.github, self.store, self.clock, on_triaged=self.suggest.retire_stale
        )
        self.engine = ApplyEngine(
            self.github,
            self.store,
            self.clock,
            pause=self.pauses,
            on_rate_limit=self.back_off,
            on_schema_changed=self.sync.recompute,
            on_triaged=self.suggest.retire_stale,
        )
        self.pending = PendingService(self.applied, self.store, self.clock)
        self.service = ApplyService(self.engine, self.applied, self.drafts, self.store, self.clock)

    async def synced(self, *issues: Issue) -> None:
        self.github.put(*issues)
        await self.sync.configure(REPO, watched=True, settings_schema=None)
        self.clock.now += timedelta(minutes=1)
        await self.sync.sync(REPO)

    def labels_of(self, n: int) -> tuple[str, ...]:
        return self.github.issues[(REPO, n)].labels

    def write_names(self) -> list[str]:
        return [name for name, _ in self.github.writes]

    def draft(self, n: int, kind: str, payload: dict[str, object], state: str = "draft") -> str:
        now = self.clock()
        sid = f"s{len(self.drafts.suggestions(SuggestionFilters(states=())))}"
        suggestion = Suggestion(
            sid,
            REPO,
            n,
            None,
            parse_kind(kind),
            payload,
            "a reason",
            "draft",
            now,
            now,
        )
        self.drafts.replace_drafts(REPO, n, [suggestion])
        if state != "draft":
            self.drafts.save(replace(suggestion, state=parse_state(state)))
        return sid


@pytest.fixture
def world(conn: sqlite3.Connection) -> World:
    return World(conn)


def one(target: Existing | New, *changes: object) -> ChangeBatch:
    return ChangeBatch.of({target: list(changes)})  # type: ignore[arg-type]


E1, E2, E3 = Existing(REPO, 1), Existing(REPO, 2), Existing(REPO, 3)


# --- Review Focus 1 and 2 -----------------------------------------------------------------------


async def test_apply_merges_with_labels_changed_on_github(world: World) -> None:
    await world.synced(issue(1, labels=("bug", "priority: medium", "area:api")))
    # Since the sync someone removed area:api and added labels the author never touched.
    current = ("bug", "priority: medium", "human-needed", "enhancement-ish")
    world.github.put(replace(world.github.issues[(REPO, 1)], labels=current))
    world.github.set_labels(*LABELS, "enhancement-ish")

    batch = one(
        E1,
        SetDimension("priority", "priority: high"),
        AddArea("area:web"),
        RemoveArea("area:api"),  # already gone on GitHub: a no-op
    )
    result = await world.engine.apply(batch)

    assert result.complete
    assert world.github.writes == [
        ("add_labels", (1, ("priority: high", "area:web"))),
        ("remove_label", (1, "priority: medium")),
    ]
    assert set(world.labels_of(1)) == {
        "bug",
        "human-needed",
        "enhancement-ish",
        "priority: high",
        "area:web",
    }
    # The cache has the labels as the write left them, re-triaged.
    cached = world.store.cached_issue(REPO, 1)
    assert cached is not None and cached.triage.priority == "priority: high"
    assert "area:web" in cached.issue.labels


async def test_batch_reports_per_issue_and_retry_resumes(world: World) -> None:
    await world.synced(issue(1), issue(2), issue(3))
    world.github.fail["add_labels:2"] = GitHubRequestError(403, "Resource not accessible")
    batch = ChangeBatch.of(
        {
            E1: [SetDimension("priority", "priority: high")],
            E2: [SetDimension("priority", "priority: high"), Close("not_planned")],
            E3: [SetDimension("priority", "priority: high")],
        }
    )

    applied = await world.service.apply(batch, origin="pending")

    per_issue = applied.result.per_issue
    assert [s.state for s in per_issue[E1].steps] == ["done"]
    assert [s.state for s in per_issue[E3].steps] == ["done"]
    failed, skipped = per_issue[E2].steps
    assert (failed.step.kind, failed.state) == ("labels", "failed")
    assert "403" in (failed.cause or "") and "Resource not accessible" in (failed.cause or "")
    assert (skipped.step.kind, skipped.state) == ("close", "skipped")
    assert not applied.result.complete

    world.github.fail.clear()
    world.github.writes.clear()
    retried = await world.service.retry(applied.id)

    assert retried.id == applied.id and retried.result.complete
    assert world.github.writes == [
        ("add_labels", (2, ("priority: high",))),
        ("close", (2, "not_planned", None)),
    ]
    stored = world.applied.batch(applied.id)
    assert stored is not None and stored.result.complete


async def test_missing_label_fails_that_step_without_creating_it(world: World) -> None:
    await world.synced(issue(1), issue(2))
    world.github.set_labels(*[name for name in LABELS if name != "priority: high"])
    batch = ChangeBatch.of(
        {E1: [SetDimension("priority", "priority: high")], New("t"): [Comment("hi")]},
        repo=[CreateIssue("t", REPO, "New issue", "", ("area:nowhere",))],
    )

    result = await world.engine.apply(batch)

    labels_step = result.per_issue[E1].steps[0]
    assert labels_step.state == "failed"
    assert "priority: high" in (labels_step.cause or "")
    assert "never creates labels" in (labels_step.cause or "")
    create = result.repo_steps[0]
    assert create.state == "failed" and "area:nowhere" in (create.cause or "")
    assert world.write_names() == []  # no add, no create, no issue

    # A label deleted on GitHub after the batch read its labels is caught by the live check.
    world.github.set_labels(*LABELS)

    def vanish(name: str, count: int) -> None:
        world.github.set_labels(*[n for n in LABELS if n != name])

    world.github.before_label = vanish
    again = await world.engine.apply(one(E2, AddArea("area:web")))
    assert again.per_issue[E2].steps[0].state == "failed"
    assert world.write_names() == []


async def test_series_order_is_labels_parent_order_close_comment(world: World) -> None:
    epic = issue(5, labels=("epic", "priority: high"), sub_issue_count=2)
    await world.synced(issue(1), issue(2), epic, issue(6, labels=("epic",)))
    world.github.parents[(REPO, 1)] = (REPO, 6)
    batch = ChangeBatch.of(
        {
            E1: [
                Comment("done here"),
                Close("completed", comment="closing"),
                SetParent(Existing(REPO, 5)),
                SetDimension("readiness", "agent-ready"),
            ],
            Existing(REPO, 5): [ReorderChild(E2, after=E1)],
        }
    )

    result = await world.engine.apply(batch)

    assert result.complete
    assert [s.step.kind for s in result.per_issue[E1].steps] == [
        "labels",
        "parent",
        "close",
        "comment",
    ]
    assert world.write_names() == [
        "add_labels",
        "add_sub_issue",
        "close",
        "comment",
        "reprioritize_sub_issue",
    ]
    # The child had another parent: it is moved, never refused.
    assert world.github.writes[1] == ("add_sub_issue", (5, 1001, True))
    assert world.github.writes[4] == ("reprioritize_sub_issue", (5, 1002, 1001))
    # One comment carries the close's comment and the comment, in order.
    assert world.github.comments_posted == [(1, "closing\n\ndone here")]
    # Writes are paced like Task 4's batches: every write after the first waits.
    assert world.pauses.calls == [WRITE_INTERVAL.total_seconds()] * 4


async def test_new_area_approval_creates_label_then_applies_it(world: World) -> None:
    await world.synced(issue(1, labels=("agent-ready", "priority: high", "bug")))
    sid = world.draft(1, "new_area", {"dimension": "area", "value": "area:sync"})

    approval = await world.service.approve(sid)

    assert approval.batch.result.complete
    assert world.write_names() == ["create_label", "add_labels"]
    assert world.github.writes[1] == ("add_labels", (1, ("area:sync",)))
    state = world.store.repo(REPO)
    assert state is not None and "area:sync" in [a.name for a in state.schema.areas]
    cached = world.store.cached_issue(REPO, 1)
    assert cached is not None and cached.triage.complete  # recomputed with the overlay
    assert approval.suggestion.state == "approved"
    assert world.drafts.suggestion(sid) == approval.suggestion


async def test_failed_label_creation_stops_dependent_issues_with_cause(world: World) -> None:
    await world.synced(issue(1), issue(2))
    world.github.fail["create_label:area:sync"] = GitHubRequestError(422, "Validation Failed")
    entry = SchemaEntry("area", "area:sync")
    batch = ChangeBatch.of(
        {E1: [AddArea("area:sync")], E2: [SetDimension("priority", "priority: high")]},
        repo=[EnsureLabel(REPO, "area:sync", "c5def5", "Area", entry)],
    )

    applied = await world.service.apply(batch, origin="suggestion")

    ensure = applied.result.repo_steps[0]
    assert ensure.state == "failed" and "Validation Failed" in (ensure.cause or "")
    dependent = applied.result.per_issue[E1].steps[0]
    assert dependent.state == "skipped"
    assert "area:sync" in (dependent.cause or "") and "Validation Failed" in (dependent.cause or "")
    assert [s.state for s in applied.result.per_issue[E2].steps] == ["done"]
    state = world.store.repo(REPO)
    assert state is not None and "area:sync" not in [a.name for a in state.schema.areas]

    world.github.fail.clear()
    world.github.writes.clear()
    retried = await world.service.retry(applied.id)
    assert retried.result.complete
    assert world.write_names() == ["create_label", "add_labels"]


async def test_approved_area_survives_a_labels_yml_refresh(world: World) -> None:
    await world.synced(issue(1, labels=("agent-ready", "priority: high", "bug")))
    sid = world.draft(1, "new_area", {"dimension": "area", "value": "area:sync"})
    await world.service.approve(sid)

    # labels.yml changes (and still doesn't list the area): the next sync re-reads it.
    world.github.files[(REPO, LABELS_YML)] = AMICUS_YML + "  - name: area:web\n"
    world.clock.now += timedelta(minutes=10)
    await world.sync.sync(REPO)

    state = world.store.repo(REPO)
    assert state is not None and state.schema_source == "repo"
    assert [a.name for a in state.schema.areas] == ["area:apple", "area:web", "area:sync"]
    cached = world.store.cached_issue(REPO, 1)
    assert cached is not None and cached.triage.complete


async def test_create_issue_then_link_it_as_a_child_in_one_batch(world: World) -> None:
    await world.synced(issue(1), issue(2))
    batch = ChangeBatch.of(
        {E1: [SetParent(New("epic"))], New("epic"): [Comment("Tracks #1")]},
        repo=[CreateIssue("epic", REPO, "An epic", "scope", ("priority: high",))],
    )

    result = await world.engine.apply(batch)

    assert result.complete
    created = result.created["epic"]
    assert (created.repo, created.n) == (REPO, 3)
    assert world.github.writes == [
        ("create_issue", ("An epic", ("priority: high",))),
        ("add_sub_issue", (3, 1001, False)),
        ("comment", (3, "Tracks #1")),
    ]
    assert world.github.parents[(REPO, 1)] == (REPO, 3)


async def test_failed_create_skips_changes_naming_its_temp_id(world: World) -> None:
    await world.synced(issue(1), issue(2))
    world.github.fail["create_issue:An epic"] = GitHubRequestError(410, "Issues are disabled")
    batch = ChangeBatch.of(
        {
            E1: [SetParent(New("epic"))],
            New("epic"): [Comment("Tracks #1")],
            E2: [SetDimension("priority", "priority: high")],
        },
        repo=[CreateIssue("epic", REPO, "An epic", "", ())],
    )

    applied = await world.service.apply(batch, origin="pending")
    result = applied.result

    assert result.repo_steps[0].state == "failed"
    for target in (E1, New("epic")):
        (step,) = result.per_issue[target].steps
        assert step.state == "skipped"
        assert "epic" in (step.cause or "") and "Issues are disabled" in (step.cause or "")
    assert [s.state for s in result.per_issue[E2].steps] == ["done"]

    world.github.fail.clear()
    retried = await world.service.retry(applied.id)
    assert retried.result.complete
    assert retried.result.created["epic"].n == 3
    assert world.github.parents[(REPO, 1)] == (REPO, 3)


# --- rate limits, unavailability and fresh reads (IR5) -----------------------------------------


async def test_a_rate_limit_stops_the_batch_and_marks_the_rest_failed(world: World) -> None:
    await world.synced(issue(1), issue(2), issue(3))
    world.github.fail["add_labels:2"] = GitHubRateLimitedError(403, "secondary rate limit", RESET)
    batch = ChangeBatch.of({E: [SetDimension("priority", "priority: high")] for E in (E1, E2, E3)})

    result = await world.engine.apply(batch)

    assert result.is_rate_limited and result.rate_limited_until == RESET
    assert [s.state for s in result.per_issue[E1].steps] == ["done"]
    assert [s.state for s in result.per_issue[E2].steps] == ["failed"]
    rest = result.per_issue[E3].steps[0]
    assert rest.state == "failed" and RESET.isoformat() in (rest.cause or "")
    assert world.write_names() == ["add_labels", "add_labels"]  # issue 3 never asked
    assert world.back_off.calls == [RESET]


async def test_a_rejected_token_stops_the_batch_too(world: World) -> None:
    await world.synced(issue(1), issue(2))
    world.github.fail["get:1"] = GitHubUnavailable("GitHub token rejected")
    batch = ChangeBatch.of({E: [Comment("x")] for E in (E1, E2)})

    result = await world.engine.apply(batch)

    assert result.stopped is not None and not result.is_rate_limited
    assert [s.state for s in result.per_issue[E2].steps] == ["failed"]
    assert world.write_names() == []


async def test_an_issue_gone_from_github_fails_its_first_step(world: World) -> None:
    await world.synced(issue(1), issue(2))
    del world.github.issues[(REPO, 1)]
    batch = ChangeBatch.of({E1: [AddArea("area:web"), Comment("x")], E2: [AddArea("area:web")]})

    result = await world.engine.apply(batch)

    first, second = result.per_issue[E1].steps
    assert first.state == "failed" and "404" in (first.cause or "")
    assert second.state == "skipped" and "earlier step" in (second.cause or "")
    assert [s.state for s in result.per_issue[E2].steps] == ["done"]


async def test_nothing_to_change_is_done_without_a_write(world: World) -> None:
    await world.synced(issue(1, labels=("bug", "priority: high", "area:web")))
    world.github.issues[(REPO, 1)] = replace(world.github.issues[(REPO, 1)], state="closed")
    result = await world.engine.apply(
        one(E1, SetDimension("priority", "priority: high"), Close("completed"), SetParent(None))
    )
    assert result.complete
    assert [s.cause for s in result.per_issue[E1].steps] == [
        "already so on GitHub",
        "already has no parent",
        "already closed",
    ]
    assert world.write_names() == []


async def test_a_parent_can_be_removed(world: World) -> None:
    await world.synced(issue(1), issue(5, labels=("epic",)))
    world.github.parents[(REPO, 1)] = (REPO, 5)
    result = await world.engine.apply(one(E1, SetParent(None)))
    assert result.complete
    assert world.github.writes == [("remove_sub_issue", (5, 1001))]


async def test_a_secondary_failure_never_fails_the_write(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1))

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(world.store, "refresh_issue", broken)
    result = await world.engine.apply(one(E1, SetDimension("priority", "priority: high")))
    assert result.complete
    assert "database is locked" in caplog.text


# --- approvals (IR16, IR18, Task 5 carries) ----------------------------------------------------


async def test_approving_a_label_draft_applies_it_and_marks_it_approved(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    approval = await world.service.approve(sid)
    assert approval.suggestion.state == "approved" and approval.suggestion.apply_error is None
    assert world.github.writes == [("add_labels", (1, ("priority: high",)))]
    with pytest.raises(SuggestionStateError):
        await world.service.approve(sid)


async def test_approving_a_readiness_gap_that_could_be_ready_sets_agent_ready(
    world: World,
) -> None:
    await world.synced(issue(1, labels=("human-needed", "bug")))
    sid = world.draft(1, "readiness_gap", {"ready": True, "missing": []})
    await world.service.approve(sid)
    assert world.github.writes == [
        ("add_labels", (1, ("agent-ready",))),
        ("remove_label", (1, "human-needed")),
    ]
    assert set(world.labels_of(1)) == {"agent-ready", "bug"}


async def test_approving_advice_only_gaps_writes_nothing(world: World) -> None:
    await world.synced(issue(1, labels=("agent-ready", "bug")))
    sid = world.draft(1, "readiness_gap", {"ready": False, "missing": ["repro steps"]})
    approval = await world.service.approve(sid)
    assert approval.suggestion.state == "approved"
    assert approval.batch.result.complete and approval.batch.result.results == ()
    assert world.github.writes == []


async def test_approving_a_duplicate_closes_it_with_the_cached_id_and_a_comment(
    world: World,
) -> None:
    await world.synced(issue(1), issue(2))
    sid = world.draft(1, "duplicate", {"repo": REPO, "n": 2, "similarity": 0.9})
    await world.service.approve(sid)
    assert world.github.writes == [
        ("close", (1, "duplicate", 1002)),
        ("comment", (1, f"Duplicate of {REPO}#2")),
    ]


async def test_approving_a_duplicate_of_an_uncached_issue_is_refused(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "duplicate", {"repo": "acme/other", "n": 9, "similarity": 0.9})
    with pytest.raises(DuplicateNotCachedError):
        await world.service.approve(sid)
    assert world.github.writes == []


async def test_approving_a_parent_links_it(world: World) -> None:
    await world.synced(issue(1), issue(5, labels=("epic",)))
    sid = world.draft(1, "parent", {"repo": REPO, "n": 5})
    await world.service.approve(sid)
    assert world.github.writes == [("add_sub_issue", (5, 1001, False))]


async def test_an_edited_draft_whose_premise_is_gone_is_refused_and_nothing_applies(
    world: World,
) -> None:
    await world.synced(issue(1))
    sid = world.draft(
        1, "label", {"dimension": "priority", "value": "priority: high"}, state="edited"
    )
    # The priority was set on GitHub since: the edited advice has nothing left to fill.
    world.github.put(replace(world.github.issues[(REPO, 1)], labels=("bug", "priority: medium")))
    world.clock.now += timedelta(minutes=10)
    await world.sync.sync(REPO)

    with pytest.raises(SuggestionStaleError):
        await world.service.approve(sid)
    assert world.github.writes == []
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "edited"


async def test_a_failed_approval_stays_approvable_with_its_cause(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    world.github.fail["add_labels:1"] = GitHubRequestError(403, "Resource not accessible")

    approval = await world.service.approve(sid)

    assert approval.suggestion.state == "draft"
    assert "Resource not accessible" in (approval.suggestion.apply_error or "")
    world.github.fail.clear()
    again = await world.service.approve(sid)
    assert again.suggestion.state == "approved" and again.suggestion.apply_error is None


async def test_retrying_an_approval_batch_marks_the_suggestion_approved(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    world.github.fail["add_labels:1"] = GitHubRequestError(403, "nope")
    approval = await world.service.approve(sid)
    world.github.fail.clear()
    await world.service.retry(approval.batch.id)
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "approved" and found.apply_error is None


# --- the pending tray --------------------------------------------------------------------------


async def test_pending_changes_stage_without_writing_and_apply_as_one_batch(world: World) -> None:
    await world.synced(issue(1), issue(2))
    first = world.pending.stage(REPO, 1, SetDimension("priority", "priority: high"), "picker")
    world.pending.stage(REPO, 2, AddArea("area:web"), "drag")
    undone = world.pending.stage(REPO, 2, Comment("oops"), "menu")
    world.pending.unstage(undone.id)
    assert world.github.writes == []
    assert [(p.target, p.source) for p in world.pending.pending()] == [
        (E1, "picker"),
        (E2, "drag"),
    ]
    assert first.change == SetDimension("priority", "priority: high")

    applied = await world.service.apply_pending(None)

    assert applied.result.complete and applied.origin == "pending"
    assert world.write_names() == ["add_labels", "add_labels"]
    assert world.pending.pending() == []
    with pytest.raises(NothingPendingError):
        await world.service.apply_pending(None)


async def test_pending_changes_are_validated_when_staged(world: World) -> None:
    await world.synced(issue(1))
    from jesinia.apps.issues.domain.changes import InvalidChangeError
    from jesinia.apps.issues.domain.triage import IssueNotCachedError

    with pytest.raises(InvalidChangeError):
        world.pending.stage(REPO, 1, SetDimension("priority", "urgent"), "picker")
    with pytest.raises(IssueNotCachedError):
        world.pending.stage(REPO, 9, Comment("x"), "menu")
    with pytest.raises(InvalidChangeError):
        world.pending.stage(REPO, 1, Comment("x"), "telepathy")
    assert world.pending.pending() == []


# --- edges of the engine ------------------------------------------------------------------------


async def test_an_ensured_label_already_on_github_is_done_without_a_write(world: World) -> None:
    await world.synced(issue(1))
    batch = ChangeBatch.of(
        {E1: [AddArea("area:web"), RemoveArea("area:none")]},
        repo=[EnsureLabel(REPO, "area:web", "c5def5", "Area")],
    )
    result = await world.engine.apply(batch)
    assert result.complete and result.repo_steps[0].cause == "already on GitHub"
    assert world.write_names() == ["add_labels"]
    assert world.pauses.calls == []  # the ensure wrote nothing, so the add isn't paced
    state = world.store.repo(REPO)
    assert state is not None and state.overlay == ()


async def test_writes_after_an_ensure_are_paced(world: World) -> None:
    await world.synced(issue(1))
    entry = SchemaEntry("area", "area:a")
    batch = ChangeBatch.of(
        {E1: [AddArea("area:a"), AddArea("area:b")]},
        repo=[
            EnsureLabel(REPO, "area:a", "c5def5", "Area", entry),
            EnsureLabel(REPO, "area:b", "c5def5", "Area"),
        ],
    )
    result = await world.engine.apply(batch)
    assert result.complete
    assert world.write_names() == ["create_label", "create_label", "add_labels"]
    assert world.pauses.calls == [WRITE_INTERVAL.total_seconds()] * 2
    assert {"area:a", "area:b"} <= {lab.name for lab in world.store.labels(REPO)}


async def test_a_rate_limit_while_ensuring_stops_the_batch(world: World) -> None:
    await world.synced(issue(1))
    world.github.fail["create_label:area:x"] = GitHubRateLimitedError(403, "limit", RESET)
    batch = ChangeBatch.of(
        {E1: [AddArea("area:x")]}, repo=[EnsureLabel(REPO, "area:x", "c5def5", "Area")]
    )
    result = await world.engine.apply(batch)
    assert result.is_rate_limited and result.rate_limited_until == RESET
    assert [r.state for r in result.results] == ["failed", "failed"]
    assert "not run" in (result.results[1].cause or "")


async def test_a_create_needing_a_label_that_failed_to_be_created_fails(world: World) -> None:
    await world.synced(issue(1))
    world.github.fail["create_label:area:x"] = GitHubRequestError(422, "nope")
    batch = ChangeBatch.of(
        {},
        repo=[
            EnsureLabel(REPO, "area:x", "c5def5", "Area"),
            CreateIssue("t", REPO, "T", "", ("area:x",)),
        ],
    )
    result = await world.engine.apply(batch)
    create = result.repo_steps[1]
    assert create.state == "failed" and "nope" in (create.cause or "")
    assert "create_issue" not in world.write_names()


async def test_an_issue_already_under_its_parent_is_left_alone(world: World) -> None:
    await world.synced(issue(1), issue(5, labels=("epic",)))
    world.github.parents[(REPO, 1)] = (REPO, 5)
    result = await world.engine.apply(one(E1, SetParent(Existing(REPO, 5))))
    assert result.per_issue[E1].steps[0].cause == "already the parent"
    assert world.write_names() == []


async def test_a_change_to_an_unwatched_repo_fails_its_step(world: World) -> None:
    await world.synced(issue(1))
    world.github.put(issue(1, repo="acme/api"))
    target = Existing("acme/api", 1)
    result = await world.engine.apply(one(target, AddArea("area:web")))
    step = result.per_issue[target].steps[0]
    assert step.state == "failed" and "not a watched repo" in (step.cause or "")


async def test_a_failing_recompute_never_fails_the_approval(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1, labels=("agent-ready", "priority: high", "bug")))

    async def broken(repo: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(world.sync, "recompute", broken)
    engine = ApplyEngine(world.github, world.store, world.clock, on_schema_changed=broken)
    entry = SchemaEntry("area", "area:sync")
    batch = ChangeBatch.of(
        {E1: [AddArea("area:sync")]},
        repo=[EnsureLabel(REPO, "area:sync", "c5def5", "Area", entry)],
    )
    result = await engine.apply(batch)
    assert result.complete
    assert "disk I/O error" in caplog.text


async def test_an_engine_without_hooks_still_applies(world: World) -> None:
    await world.synced(issue(1))
    engine = ApplyEngine(world.github, world.store, world.clock, pause=world.pauses)
    entry = SchemaEntry("area", "area:sync")
    batch = ChangeBatch.of(
        {E1: [AddArea("area:sync")]},
        repo=[EnsureLabel(REPO, "area:sync", "c5def5", "Area", entry)],
    )
    assert (await engine.apply(batch)).complete


async def test_retrying_an_approval_already_settled_leaves_it_alone(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    world.github.fail["add_labels:1"] = GitHubRequestError(403, "nope")
    approval = await world.service.approve(sid)
    world.suggest.dismiss(sid)
    world.github.fail.clear()
    retried = await world.service.retry(approval.batch.id)
    assert retried.result.complete
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "dismissed"


async def test_removing_a_label_alone_makes_no_add(world: World) -> None:
    await world.synced(issue(1, labels=("bug", "area:web")))
    result = await world.engine.apply(one(E1, RemoveArea("area:web")))
    assert result.complete
    assert world.github.writes == [("remove_label", (1, "area:web"))]


def test_deleting_no_pending_changes_deletes_nothing(world: World) -> None:
    assert world.applied.delete_pending([]) == 0
