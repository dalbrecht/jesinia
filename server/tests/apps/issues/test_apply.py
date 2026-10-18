# SPDX-License-Identifier: AGPL-3.0-or-later
"""The apply engine and the pending-changes tray (spec §6.1, §6.2, §8; plan Task 6).

Each issue's changes run as one ordered series against a fresh read: labels as add/remove (never
a replace), then the parent link, the sub-issue order, the close and the comment. A series stops
at its first failure and Retry runs only what didn't apply. Labels are never created implicitly
(IR4): ``EnsureLabel`` goes through Task 4's executor (IR12) and every label is checked live
before it is added. A rate limit stops the batch (IR5).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

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
    InvalidChangeError,
    New,
    NothingPendingError,
    RemoveArea,
    ReorderChild,
    SetDimension,
    SetParent,
)
from jesinia.apps.issues.domain.schema import SchemaEntry, parse_labels_yml
from jesinia.apps.issues.domain.suggestions import (
    DuplicateNotCachedError,
    Suggestion,
    SuggestionFilters,
    SuggestionStaleError,
    SuggestionStateError,
    parse_kind,
    parse_state,
)
from jesinia.apps.issues.domain.triage import triage
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
from jesinia.services.github.domain.issue import CloseReason, CommentRef, Issue, IssueRef
from tests.apps.issues.test_labels import AMICUS_YML, BackOff, FakeGitHub, Pauses
from tests.apps.issues.test_sync import REPO, FakeClock, issue

RESET = datetime(2026, 10, 1, 13, 0).astimezone()
AMICUS = parse_labels_yml(AMICUS_YML)
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
        # Set both to hold ``add_labels`` until ``release`` (a write still in flight).
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def add_labels(self, repo: str, n: int, names: Sequence[str]) -> tuple[str, ...]:
        if self.entered is not None and self.release is not None:
            self.entered.set()
            await self.release.wait()
        return await super().add_labels(repo, n, names)

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
    world.github.parents[(REPO, 2)] = (REPO, 5)
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


# --- fix round 1: conflicts, claims and fresh-read premises (IR39–IR41, I1, I2) ---------------


class Held(FakeWrites):
    """``FakeWrites`` whose ``add_labels`` waits for ``release`` (a write held mid-flight)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def started(self) -> asyncio.Event:
        return cast(asyncio.Event, self.entered)

    @property
    def go(self) -> asyncio.Event:
        return cast(asyncio.Event, self.release)


async def test_a_staged_parent_move_refuses_when_github_moved_it(world: World) -> None:
    await world.synced(issue(1), issue(5, labels=("epic",)), issue(6, labels=("epic",)))
    staged = world.pending.stage(REPO, 1, SetParent(Existing(REPO, 5)), "drag")
    assert staged.change == SetParent(Existing(REPO, 5), expects_parent=True)
    world.github.parents[(REPO, 1)] = (REPO, 6)  # someone moved it on GitHub since

    applied = await world.service.apply_pending(None)

    (step,) = applied.result.per_issue[E1].steps
    assert step.state == "failed" and step.is_conflict
    assert "moved on GitHub" in (step.cause or "") and f"{REPO}#6" in (step.cause or "")
    assert world.write_names() == []


async def test_a_staged_parent_move_applies_when_github_agrees(world: World) -> None:
    await world.synced(issue(1, parent=IssueRef(REPO, 6, 1006)), issue(5), issue(6))
    world.github.parents[(REPO, 1)] = (REPO, 6)
    staged = world.pending.stage(REPO, 1, SetParent(None), "menu")
    assert staged.change == SetParent(None, expects_parent=True, expected_parent=Existing(REPO, 6))
    applied = await world.service.apply_pending(None)
    assert applied.result.complete
    assert world.github.writes == [("remove_sub_issue", (6, 1001))]


async def test_a_reorder_whose_child_left_the_epic_is_skipped_with_a_cause(world: World) -> None:
    await world.synced(issue(1), issue(2), issue(5, labels=("epic",), sub_issue_count=2))
    world.github.parents[(REPO, 2)] = (REPO, 5)
    batch = ChangeBatch.of(
        {Existing(REPO, 5): [ReorderChild(E1, after=E2), Comment("reordered")]}
    )  # issue 1 isn't (or is no longer) a child of #5
    result = await world.engine.apply(batch)
    order, comment = result.per_issue[Existing(REPO, 5)].steps
    assert order.state == "skipped" and f"{REPO}#1" in (order.cause or "")
    assert "isn't a sub-issue" in (order.cause or "")
    assert comment.state == "done"  # a skipped reorder doesn't stop the epic's series
    assert "reprioritize_sub_issue" not in world.write_names()


async def test_an_approval_in_flight_survives_retiring_and_reruns(conn: sqlite3.Connection) -> None:
    world = World(conn)
    held = Held()
    held.files, held.repo_labels = world.github.files, world.github.repo_labels
    world.github = held
    world.engine = ApplyEngine(held, world.store, world.clock, pause=world.pauses)
    world.service = ApplyService(
        world.engine, world.applied, world.drafts, world.store, world.clock
    )
    world.sync = SyncService(held, world.store, world.clock)
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})

    approving = asyncio.create_task(world.service.approve(sid))
    await held.started.wait()
    # Meanwhile a sync retires drafts whose premise went, and a rerun replaces untouched ones.
    filled = replace(held.issues[(REPO, 1)], labels=("bug", "priority: high"))
    world.suggest.retire_stale(REPO, [(filled, triage(filled.labels, 0, AMICUS))])
    world.drafts.replace_drafts(REPO, 1, [])
    held.go.set()
    approval = await approving

    assert approval.suggestion.state == "approved"
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "approved"


async def test_approval_refuses_when_github_filled_the_dimension_since_sync(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    github_now = replace(world.github.issues[(REPO, 1)], labels=("bug", "priority: medium"))
    world.github.put(github_now)  # not synced yet

    with pytest.raises(SuggestionStaleError) as raised:
        await world.service.approve(sid)

    assert "changed on GitHub" in str(raised.value)
    assert world.write_names() == []
    assert world.labels_of(1) == ("bug", "priority: medium")
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "draft" and found.apply_error


async def test_approval_refuses_when_github_gave_it_a_parent_since_sync(world: World) -> None:
    await world.synced(issue(1), issue(5, labels=("epic",)), issue(6, labels=("epic",)))
    sid = world.draft(1, "parent", {"repo": REPO, "n": 5})
    world.github.parents[(REPO, 1)] = (REPO, 6)
    with pytest.raises(SuggestionStaleError):
        await world.service.approve(sid)
    assert world.write_names() == []


async def test_approval_refuses_when_github_closed_it_since_sync(world: World) -> None:
    await world.synced(issue(1), issue(2))
    sid = world.draft(1, "duplicate", {"repo": REPO, "n": 2, "similarity": 0.9})
    world.github.put(replace(world.github.issues[(REPO, 1)], state="closed"))
    with pytest.raises(SuggestionStaleError):
        await world.service.approve(sid)
    assert world.write_names() == []  # no "Duplicate of" comment on an issue closed otherwise


async def test_a_duplicate_of_itself_is_refused(world: World) -> None:
    await world.synced(issue(1))
    with pytest.raises(InvalidChangeError):
        world.pending.stage(REPO, 1, Close("duplicate", 1001, E1), "menu")
    sid = world.draft(1, "duplicate", {"repo": REPO, "n": 1, "similarity": 1.0})
    with pytest.raises(InvalidChangeError):
        await world.service.approve(sid)
    assert world.write_names() == []


async def test_a_close_comment_is_not_posted_when_the_close_was_a_no_op(world: World) -> None:
    await world.synced(issue(1))
    world.github.put(replace(world.github.issues[(REPO, 1)], state="closed"))
    result = await world.engine.apply(
        one(E1, Close("not_planned", comment="won't do"), Comment("noted"))
    )
    assert result.complete
    assert world.github.comments_posted == [(1, "noted")]
    only_close = await world.engine.apply(one(E1, Close("not_planned", comment="won't do")))
    assert only_close.per_issue[E1].steps[1].cause == "nothing to post: it was already closed"


async def test_an_apply_waits_out_a_known_rate_limit(world: World) -> None:
    await world.synced(issue(1))
    engine = ApplyEngine(world.github, world.store, world.clock, blocked_until=lambda: RESET)
    world.clock.now = RESET - timedelta(minutes=1)
    result = await engine.apply(one(E1, Comment("x")))
    assert result.is_rate_limited and result.rate_limited_until == RESET
    assert world.github.calls[-1][0] != "get" and world.write_names() == []


async def test_a_partly_applied_labels_step_caches_what_landed(world: World) -> None:
    await world.synced(issue(1, labels=("bug", "priority: medium")))
    world.github.fail["remove_label:1"] = GitHubRequestError(403, "nope")
    result = await world.engine.apply(one(E1, SetDimension("priority", "priority: high")))
    assert not result.complete
    cached = world.store.cached_issue(REPO, 1)
    assert cached is not None and "priority: high" in cached.issue.labels


async def test_a_failing_overlay_write_fails_the_ensure_step(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.synced(issue(1))

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(world.store, "add_overlay", broken)
    entry = SchemaEntry("area", "area:x")
    batch = ChangeBatch.of(
        {E1: [AddArea("area:x")]}, repo=[EnsureLabel(REPO, "area:x", "c5def5", "Area", entry)]
    )
    result = await world.engine.apply(batch)
    ensure = result.repo_steps[0]
    assert ensure.state == "failed" and "database is locked" in (ensure.cause or "")
    assert result.per_issue[E1].steps[0].state == "skipped"


async def test_a_failing_batch_store_still_returns_the_result(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("x"), "menu")
    started = world.service.begin_pending(None, key=None)  # the write-ahead row is stored
    # The database breaks after that: each step's record and the final one fail.

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(world.applied, "save_batch", broken)
    applied = await world.service._run(started, None)
    assert applied.result.complete and "disk full" in caplog.text
    assert world.pending.pending() == []  # the writes happened: never re-applied


async def test_approving_a_suggestion_without_an_issue_says_it_cant_be_yet(
    world: World, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO suggestions (id, scope, kind, payload_json, reason, state, created_at, "
        "updated_at) VALUES ('g1', 'queue:mine', 'group', '{}', 'r', 'draft', ?, ?)",
        (world.clock().isoformat(), world.clock().isoformat()),
    )
    with pytest.raises(InvalidChangeError):
        await world.service.approve("g1")


async def test_claims_are_released_and_only_open_drafts_are_claimed(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    assert world.drafts.claim(sid, world.clock())
    assert not world.drafts.claim(sid, world.clock())  # one approval at a time
    assert world.drafts.release_all() == 1
    world.suggest.dismiss(sid)
    assert not world.drafts.claim(sid, world.clock())


async def test_a_stale_claim_blocks_a_second_approval(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    assert world.drafts.claim(sid, world.clock())
    with pytest.raises(SuggestionStateError, match="being approved"):
        await world.service.approve(sid)
    assert world.write_names() == []


async def test_a_failing_store_on_retry_still_returns_the_result(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1))
    world.github.fail["comment:1"] = GitHubRequestError(500, "boom")
    applied = await world.service.apply(one(E1, Comment("x")), origin="pending")
    world.github.fail.clear()

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("disk full")

    again = world.service.begin_retry(applied.id)
    monkeypatch.setattr(world.applied, "save_batch", broken)
    retried = await world.service._run(again, None, resume=True)
    assert retried.result.complete and "disk full" in caplog.text


async def test_the_engine_reads_the_sync_back_off(world: World) -> None:
    await world.synced(issue(1))
    assert world.sync.backoff_until() is None
    world.sync.back_off(RESET)
    assert world.sync.backoff_until() == RESET


def test_a_corrupt_overlay_row_is_a_typed_error(world: World, conn: sqlite3.Connection) -> None:
    world.store.watch(REPO, world.clock())
    conn.execute(
        "INSERT INTO schema_overlay (repo, dimension, canonical, approved_at) "
        "VALUES (?, 'mood', 'x', '2026-10-01')",
        (REPO,),
    )
    with pytest.raises(InvalidChangeError):
        world.store.repo(REPO)


# --- fix round 2: readiness premise, retry claims, the back-off lapsing -------------------------


async def test_approval_refuses_when_github_changed_readiness_since_sync(world: World) -> None:
    await world.synced(issue(1, labels=("human-needed", "bug")))
    sid = world.draft(1, "readiness_gap", {"ready": True, "missing": []})
    world.github.put(replace(world.github.issues[(REPO, 1)], labels=("design needed", "bug")))

    with pytest.raises(SuggestionStaleError) as raised:
        await world.service.approve(sid)

    assert "readiness" in str(raised.value) and "design needed" in str(raised.value)
    assert world.write_names() == []
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "draft" and found.apply_error


async def test_a_retried_approval_claims_its_suggestion(conn: sqlite3.Connection) -> None:
    world = World(conn)
    held = Held()
    held.files, held.repo_labels = world.github.files, world.github.repo_labels
    world.github = held
    world.engine = ApplyEngine(held, world.store, world.clock, pause=world.pauses)
    world.service = ApplyService(
        world.engine, world.applied, world.drafts, world.store, world.clock
    )
    world.sync = SyncService(held, world.store, world.clock)
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    held.fail["get:1"] = GitHubRequestError(502, "flaky")
    approval = await world.service.approve(sid)
    held.fail.clear()

    retrying = asyncio.create_task(world.service.retry(approval.batch.id))
    await held.started.wait()
    assert not world.drafts.claim(sid, world.clock())  # the retry holds it
    world.drafts.replace_drafts(REPO, 1, [])
    held.go.set()
    await retrying

    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "approved"
    assert world.drafts.claim(sid, world.clock()) is False  # approved: nothing to claim


async def test_a_failing_release_keeps_the_applys_own_outcome(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})

    def broken(suggestion_id: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(world.drafts, "release", broken)
    approval = await world.service.approve(sid)
    assert approval.suggestion.state == "approved"
    assert "database is locked" in caplog.text


async def test_an_apply_runs_once_the_back_off_lapses(world: World) -> None:
    await world.synced(issue(1))
    engine = ApplyEngine(world.github, world.store, world.clock, blocked_until=lambda: RESET)
    world.clock.now = RESET - timedelta(minutes=1)
    calls = len(world.github.calls)
    blocked = await engine.apply(one(E1, Comment("x")))
    assert blocked.is_rate_limited and len(world.github.calls) == calls  # asked GitHub nothing
    world.clock.now = RESET + timedelta(seconds=1)
    assert (await engine.apply(one(E1, Comment("x")))).complete


# --- PR 2 final fix wave: write-ahead batches, 202 and polling, replays (IR45, IR46) -----------


def held_world(conn: sqlite3.Connection) -> tuple[World, Held]:
    world = World(conn)
    held = Held()
    held.files, held.repo_labels = world.github.files, world.github.repo_labels
    world.github = held
    world.engine = ApplyEngine(held, world.store, world.clock, pause=world.pauses)
    world.service = ApplyService(
        world.engine, world.applied, world.drafts, world.store, world.clock
    )
    world.sync = SyncService(held, world.store, world.clock)
    return world, held


async def test_a_started_batch_is_stored_running_before_any_write(conn: sqlite3.Connection) -> None:
    world, held = held_world(conn)
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, SetDimension("priority", "priority: high"), "picker")

    started = world.service.start_pending(None, key="k1")

    assert started.status == "running" and world.pending.pending() == []
    stored = world.applied.batch(started.id)
    assert stored is not None and stored.status == "running"
    assert [r.cause for r in stored.result.results] == ["not run yet"]
    await held.started.wait()
    assert world.write_names() == []
    held.go.set()
    await world.service.drain()
    done = world.applied.batch(started.id)
    assert done is not None and done.status == "finished" and done.result.complete


async def test_a_stop_mid_batch_is_interrupted_and_retry_never_reposts(
    conn: sqlite3.Connection,
) -> None:
    world, held = held_world(conn)
    await world.synced(issue(1), issue(2))
    world.pending.stage(REPO, 1, Comment("posted once"), "menu")
    world.pending.stage(REPO, 2, SetDimension("priority", "priority: high"), "picker")
    started = world.service.start_pending(None, key=None)
    await held.started.wait()  # issue 1's comment went out; issue 2's write is in flight

    await world.service.stop()

    stopped = world.applied.batch(started.id)
    assert stopped is not None and stopped.status == "interrupted"
    assert [r.state for r in stopped.result.results] == ["done", "skipped"]
    held.go.set()
    world.service = ApplyService(
        world.engine, world.applied, world.drafts, world.store, world.clock
    )
    retried = await world.service.retry(started.id)
    assert retried.status == "finished" and retried.result.complete
    assert held.comments_posted == [(1, "posted once")]


async def test_start_marks_batches_a_crash_left_running_interrupted(world: World) -> None:
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("x"), "menu")
    started = world.service.begin_pending(None, key=None)  # stored, never run
    assert world.service.recover() == 1
    found = world.applied.batch(started.id)
    assert found is not None and found.status == "interrupted"
    assert (await world.service.retry(started.id)).result.complete


async def test_a_running_batch_cant_be_retried(conn: sqlite3.Connection) -> None:
    from jesinia.apps.issues.domain.changes import BatchRunningError

    world, held = held_world(conn)
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, SetDimension("priority", "priority: high"), "picker")
    started = world.service.start_pending(None, key=None)
    with pytest.raises(BatchRunningError):
        world.service.start_retry(started.id)
    held.go.set()
    await world.service.drain()


async def test_a_keyed_apply_maps_to_its_batch_and_never_applies_the_tray_again(
    world: World,
) -> None:
    await world.synced(issue(1), issue(2))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    first = world.service.begin_pending(None, key="key-1")
    world.pending.stage(REPO, 2, Comment("B"), "menu")
    again = world.service.begin_pending(None, key="key-1")
    assert again.id == first.id
    assert [p.target for p in world.pending.pending()] == [E2]  # B is still pending


async def test_a_keyed_approval_maps_to_its_batch(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    first = world.service.start_approval(sid, key="key-2")
    await world.service.drain()
    again = world.service.start_approval(sid, key="key-2")
    assert again.batch.id == first.batch.id and again.suggestion.state == "approved"
    assert world.write_names() == ["add_labels"]


async def test_a_stop_before_an_approval_runs_releases_its_claim(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    started = world.service.start_approval(sid, key=None)
    await world.service.stop()  # cancelled before it ever ran
    assert world.drafts.claim(sid, world.clock())  # released
    found = world.applied.batch(started.batch.id)
    assert found is not None and found.status == "interrupted"


async def test_a_retry_withholds_a_close_comment_the_first_run_withheld(world: World) -> None:
    await world.synced(issue(1))
    world.github.put(replace(world.github.issues[(REPO, 1)], state="closed"))
    world.github.fail["comment:1"] = GitHubRateLimitedError(403, "limit", None)
    applied = await world.service.apply(
        one(E1, Close("not_planned", comment="won't do"), Comment("noted")), origin="pending"
    )
    assert [r.cause for r in applied.result.per_issue[E1].steps][0] == "already closed"
    world.github.fail.clear()
    await world.service.retry(applied.id)
    assert world.github.comments_posted == [(1, "noted")]


async def test_edits_and_dismissals_wait_for_an_approval_in_flight(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    assert world.drafts.claim(sid, world.clock())
    with pytest.raises(SuggestionStateError, match="being approved"):
        world.suggest.edit(sid, {"dimension": "priority", "value": "priority: medium"}, None)
    with pytest.raises(SuggestionStateError, match="being approved"):
        world.suggest.dismiss(sid)


async def test_an_unexpected_step_error_fails_that_step_and_the_batch_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1), issue(2))

    async def broken(repo: str, n: int, body: str) -> CommentRef:
        raise RuntimeError("boom")

    monkeypatch.setattr(world.github, "comment", broken)
    result = await world.engine.apply(
        ChangeBatch.of({E1: [Comment("x")], E2: [SetDimension("priority", "priority: high")]})
    )
    (failed,) = result.per_issue[E1].steps
    assert failed.state == "failed" and "RuntimeError" in (failed.cause or "")
    assert [s.state for s in result.per_issue[E2].steps] == ["done"]
    assert "boom" in caplog.text


async def test_a_failing_progress_record_never_stops_the_engine(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1))

    def broken(result: object) -> None:
        raise sqlite3.OperationalError("disk full")

    result = await world.engine.apply(one(E1, Comment("x")), on_step=broken)
    assert result.complete and "recording a step's progress failed" in caplog.text


async def test_a_replayed_approval_whose_suggestion_is_gone_is_not_found(
    world: World, conn: sqlite3.Connection
) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    world.service.start_approval(sid, key="k")
    await world.service.drain()
    conn.execute("DELETE FROM suggestions WHERE id = ?", (sid,))
    from jesinia.apps.issues.domain.suggestions import SuggestionNotFoundError

    with pytest.raises(SuggestionNotFoundError):
        world.service.start_approval(sid, key="k")


async def test_a_failing_write_ahead_releases_the_claim_and_writes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(world.applied, "save_batch", broken)
    with pytest.raises(sqlite3.OperationalError):
        world.service.start_approval(sid, key=None)
    assert not world.drafts.is_claimed(sid) and world.write_names() == []


async def test_a_background_batch_that_breaks_is_interrupted_and_logged(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("engine bug")

    monkeypatch.setattr(world.engine, "apply", broken)
    started = world.service.start_approval(sid, key=None)
    await world.service.drain()
    found = world.applied.batch(started.batch.id)
    assert found is not None and found.status == "interrupted"
    assert not world.drafts.is_claimed(sid) and "engine bug" in caplog.text

    world.pending.stage(REPO, 1, Comment("x"), "menu")
    again = world.service.start_pending(None, key=None)

    def unreadable(batch_id: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(world.applied, "batch", unreadable)
    await world.service.drain()
    assert "marking batch" in caplog.text and again.status == "running"


# --- PR 2 re-review: replays never re-run a batch (N1), in-flight writes (N2), retries (N3–N5) --


class HeldComments(FakeWrites):
    """``FakeWrites`` whose ``comment`` waits for ``go`` before it posts (a write in flight)."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.go = asyncio.Event()

    async def comment(self, repo: str, n: int, body: str) -> CommentRef:
        self.started.set()
        await self.go.wait()
        return await super().comment(repo, n, body)


async def test_replaying_a_finished_keyed_batch_posts_nothing_more(world: World) -> None:
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    first = world.service.start_pending(None, key="apply:k")
    await world.service.drain()
    again = world.service.start_pending(None, key="apply:k")
    await world.service.drain()
    assert again.id == first.id and again.status == "finished"
    assert world.github.comments_posted == [(1, "A")]


async def test_replaying_an_interrupted_keyed_batch_posts_nothing_more(world: World) -> None:
    await world.synced(issue(1), issue(2))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    world.pending.stage(REPO, 2, Comment("B"), "menu")
    first = world.service.start_pending(None, key="apply:k")
    await world.service.drain()
    stored = world.applied.batch(first.id)
    assert stored is not None
    world.applied.save_batch(replace(stored, status="interrupted"))
    again = world.service.start_pending(None, key="apply:k")
    await world.service.drain()
    assert again.status == "interrupted"  # resuming is Retry's job, never a replay's
    assert world.github.comments_posted == [(1, "A"), (2, "B")]


async def test_a_replay_after_a_crash_before_the_key_completed_never_reruns(world: World) -> None:
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    written = world.service.begin_pending(None, key="apply:k")  # the row, then the crash
    restarted = ApplyService(world.engine, world.applied, world.drafts, world.store, world.clock)
    assert restarted.recover() == 1
    again = restarted.start_pending(None, key="apply:k")
    await restarted.drain()
    assert again.id == written.id and again.status == "interrupted"
    assert world.github.comments_posted == []


async def test_a_replay_of_a_running_batch_with_no_task_resumes_it_once(world: World) -> None:
    await world.synced(issue(1), issue(2))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    world.pending.stage(REPO, 2, Comment("B"), "menu")
    written = world.service.begin_pending(None, key="apply:k")
    # The first comment went out and was recorded, then the task was lost.
    (first, second) = written.result.results
    done = replace(written.result, results=(replace(first, state="done", cause=None), second))
    world.applied.save_batch(replace(written, result=done))
    world.github.comments_posted.append((1, "A"))
    again = world.service.start_pending(None, key="apply:k")
    await world.service.drain()
    final = world.applied.batch(again.id)
    assert final is not None and final.result.complete
    assert world.github.comments_posted == [(1, "A"), (2, "B")]


async def test_a_comment_cut_off_in_flight_is_never_reposted_blindly(
    conn: sqlite3.Connection,
) -> None:
    world = World(conn)
    held = HeldComments()
    held.files, held.repo_labels = world.github.files, world.github.repo_labels
    world.github = held
    world.engine = ApplyEngine(held, world.store, world.clock, pause=world.pauses)
    world.service = ApplyService(
        world.engine, world.applied, world.drafts, world.store, world.clock
    )
    world.sync = SyncService(held, world.store, world.clock)
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    started = world.service.start_pending(None, key=None)
    await held.started.wait()
    await world.service.stop()  # cut off while the comment may be on its way
    held.go.set()

    first = await world.service.retry(started.id)
    (step,) = first.result.results
    assert step.state == "failed" and "check GitHub" in (step.cause or "")
    assert held.comments_posted == []
    second = await world.service.retry(started.id)  # the author checked: now it is posted
    assert second.result.complete and held.comments_posted == [(1, "A")]


async def test_a_retry_while_the_batch_still_has_a_task_is_refused(world: World) -> None:
    from jesinia.apps.issues.domain.changes import BatchRunningError

    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("A"), "menu")
    started = world.service.start_pending(None, key=None)
    stored = world.applied.batch(started.id)
    assert stored is not None
    world.applied.save_batch(replace(stored, status="finished"))  # finished, cache update left
    with pytest.raises(BatchRunningError):
        world.service.start_retry(started.id)
    await world.service.drain()


async def test_one_key_on_two_approvals_is_two_approvals(world: World) -> None:
    await world.synced(issue(1), issue(2))
    first = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    second = world.draft(2, "label", {"dimension": "priority", "value": "priority: high"})
    a = world.service.start_approval(first, key="same")
    b = world.service.start_approval(second, key="same")
    await world.service.drain()
    assert a.batch.id != b.batch.id
    found = world.drafts.suggestion(second)
    assert found is not None and found.state == "approved"


async def test_starting_a_tray_batch_is_one_transaction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.synced(issue(1))
    world.pending.stage(REPO, 1, Comment("A"), "menu")

    def broken(ids: object) -> int:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(world.applied, "_delete", broken)
    with pytest.raises(sqlite3.OperationalError):
        world.service.begin_pending(None, key="apply:k")
    assert world.applied.batch_by_key("apply:k") is None  # no batch without its rows taken
    assert len(world.pending.pending()) == 1


async def test_a_replayed_approval_running_with_no_task_resumes_once(world: World) -> None:
    await world.synced(issue(1))
    sid = world.draft(1, "label", {"dimension": "priority", "value": "priority: high"})
    written = world.service.begin_approval(sid, key="k")  # the row, then the task was lost
    assert world.drafts.release_all() == 1  # as a restart releases claims
    again = world.service.start_approval(sid, key="k")
    await world.service.drain()
    assert again.batch.id == written.batch.id
    assert world.write_names() == ["add_labels"]
    found = world.drafts.suggestion(sid)
    assert found is not None and found.state == "approved" and not world.drafts.is_claimed(sid)
