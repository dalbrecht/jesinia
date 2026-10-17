# SPDX-License-Identifier: AGPL-3.0-or-later
"""The label setup and migration batches (spec §2, §6.2): previewed, then applied with an
approval through the ordered ``LabelOp`` executor. Labels are read fresh before anything is
created or relabelled (IR4); a migration lists each old label's issues and pull requests live
before deleting it (IR31); writes are paced and a rate limit stops the batch (IR5); old labels
are deleted only after every move succeeded (IR12); the cache is updated at once (IR32)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jesinia.apps.issues.application.labels import (
    WRITE_INTERVAL,
    LabelExecutor,
    LabelSetupService,
)
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.sync import LABELS_YML, SyncService
from jesinia.apps.issues.domain.labels import (
    COMMA_CAUSE,
    PLAN_TTL,
    CreateLabel,
    DeleteLabel,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanNotFoundError,
    LabelPlanOutdatedError,
    SchemaHasErrorsError,
    skip_cause,
)
from jesinia.apps.issues.domain.triage import IssueFilters, RepoNotWatchedError
from jesinia.apps.issues.infrastructure.store import SqliteIssuesStore
from jesinia.apps.issues.module import ISSUES_DB
from jesinia.core.db import open_db
from jesinia.services.github.application.service import (
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.issue import (
    InvalidLabelError,
    Label,
    LabeledItem,
    validate_label_filter,
)
from tests.apps.issues.test_sync import REPO, FakeClock, FakeIssues, issue

AMICUS_YML = """\
readiness:
  - agent-ready
  - human-needed
  - information-needed
  - name: design-needed
    aliases: [design needed]
  - blocked
  - icebox
areas:
  - name: area:apple
    aliases: [platform:ios, apple]
"""
RESET = datetime(2026, 10, 1, 13, 0).astimezone()


class FakeGitHub(FakeIssues):
    """``FakeIssues`` plus the label writes, applied to the in-memory repo, and pull requests
    (``prs``: number -> labels), which sync never caches. ``fail`` maps a write
    (``"create_label:name"``, ``"add_labels:n"``, ...) to the error it raises."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, tuple[object, ...]]] = []
        self.fail: dict[str, Exception] = {}
        self.labels_error: Exception | None = None
        self.prs: dict[int, tuple[str, ...]] = {}
        # Called before each ``labeled`` answer with the name and how many times it was listed
        # (1-based): mutate the repo to race the batch.
        self.before_labeled: Callable[[str, int], None] | None = None

    def label_names(self) -> list[str]:
        return [label.name for label in self.repo_labels.get(REPO, [])]

    def set_labels(self, *names: str) -> None:
        self.repo_labels[REPO] = [Label(name, "ededed", "") for name in names]

    def labels_on(self, n: int) -> tuple[str, ...]:
        return self.prs[n] if n in self.prs else self.issues[(REPO, n)].labels

    def _set(self, n: int, labels: tuple[str, ...]) -> tuple[str, ...]:
        if n in self.prs:
            self.prs[n] = labels
        else:
            self.issues[(REPO, n)] = replace(self.issues[(REPO, n)], labels=labels)
        return labels

    def _write(self, name: str, key: object, *args: object) -> None:
        self.writes.append((name, args))
        error = self.fail.get(f"{name}:{key}")
        if error is not None:
            raise error

    async def labels(self, repo: str) -> list[Label]:
        if self.labels_error is not None:
            raise self.labels_error
        return await super().labels(repo)

    async def labeled(self, repo: str, name: str) -> list[LabeledItem]:
        self.calls.append(("labeled", (repo, name)))
        validate_label_filter(name)
        if self.before_labeled is not None:
            self.before_labeled(name, self.calls.count(("labeled", (repo, name))))
        error = self.fail.get(f"labeled:{name}")
        if error is not None:
            raise error
        wanted = name.casefold()
        found = [
            LabeledItem(i.number, i.labels, False)
            for i in sorted(self.issues.values(), key=lambda i: i.number)
            if i.repo == repo and wanted in {label.casefold() for label in i.labels}
        ]
        found += [
            LabeledItem(n, labels, True)
            for n, labels in sorted(self.prs.items())
            if wanted in {label.casefold() for label in labels}
        ]
        return found

    async def add_labels(self, repo: str, n: int, names: Sequence[str]) -> tuple[str, ...]:
        self._write("add_labels", n, n, tuple(names))
        return self._set(n, (*self.labels_on(n), *names))

    async def remove_label(self, repo: str, n: int, name: str) -> tuple[str, ...]:
        self._write("remove_label", n, n, name)
        return self._set(n, tuple(label for label in self.labels_on(n) if label != name))

    async def create_label(self, repo: str, name: str, color: str, description: str) -> Label:
        self._write("create_label", name, name)
        label = Label(name, color, description)
        self.repo_labels.setdefault(repo, []).append(label)
        return label

    async def delete_label(self, repo: str, name: str) -> None:
        self._write("delete_label", name, name)
        self.repo_labels[repo] = [lab for lab in self.repo_labels[repo] if lab.name != name]
        for n in [*self.prs, *(number for (_, number) in self.issues)]:
            self._set(n, tuple(label for label in self.labels_on(n) if label != name))


class Pauses:
    """The executor's write pacing, recorded instead of slept."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class BackOff:
    def __init__(self) -> None:
        self.calls: list[datetime | None] = []

    def __call__(self, until: datetime | None) -> None:
        self.calls.append(until)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, ISSUES_DB)
    yield connection
    connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SqliteIssuesStore:
    return SqliteIssuesStore(conn)


@pytest.fixture
def github() -> FakeGitHub:
    fake = FakeGitHub()
    fake.files[(REPO, LABELS_YML)] = AMICUS_YML
    return fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def back_off() -> BackOff:
    return BackOff()


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


@pytest.fixture
def labels(
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    back_off: BackOff,
    pauses: Pauses,
) -> LabelSetupService:
    return LabelSetupService(github, store, clock, on_rate_limit=back_off, pause=pauses)


def executor(github: FakeGitHub, pauses: Pauses | None = None) -> LabelExecutor:
    return LabelExecutor(github, pause=pauses or Pauses())


async def synced(github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock) -> None:
    sync = SyncService(github, store, clock)
    await sync.configure(REPO, watched=True, settings_schema=None)
    clock.now += timedelta(minutes=1)
    await sync.sync(REPO)
    github.calls.clear()


# Setup --------------------------------------------------------------------------------


async def test_setup_creates_only_missing_canonical_labels(
    labels: LabelSetupService,
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    pauses: Pauses,
) -> None:
    present = ["agent-ready", "human-needed", "information-needed", "design needed", "blocked",
               "icebox", "priority: high", "priority: medium", "priority: low", "bug",
               "enhancement", "tech-debt", "test", "performance", "security", "apple"]  # fmt: skip
    github.set_labels(*present)
    await synced(github, store, clock)

    plan = await labels.preview(REPO, "setup")
    names = [op.name for op in plan.ops if isinstance(op, CreateLabel)]
    assert names == ["documentation", "epic", "initiative"]
    assert len(plan.ops) == 3 and plan.expires_at == clock.now + PLAN_TTL
    assert github.writes == []  # a preview never writes

    github.calls.clear()
    run = await labels.apply(REPO, "setup", plan.id)
    assert run.complete and [r.state for r in run.results] == ["done"] * 3
    assert [w for w, _ in github.writes] == ["create_label"] * 3
    # IR4: the schema and the labels were read fresh before anything was created, once each.
    assert github.call_names() == ["file", "labels"]
    assert (await labels.planner.current(REPO, "setup")).ops == ()
    # IR5: one pause between consecutive writes.
    assert pauses.calls == [WRITE_INTERVAL.total_seconds()] * 2


async def test_setup_skips_a_label_that_exists_by_the_time_it_runs(github: FakeGitHub) -> None:
    github.set_labels("bug")
    run = await executor(github).run(
        REPO, [CreateLabel("Bug", "1d76db", "Type"), CreateLabel("epic", "5319e7", "H")]
    )
    assert run.complete and [r.cause for r in run.results] == ["already on GitHub", None]
    assert github.writes == [("create_label", ("epic",))]
    assert [label.name for label in run.labels] == ["bug", "epic"]


async def test_apply_refuses_when_labels_changed_since_the_preview(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("bug")
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "setup")
    github.set_labels("bug", "epic")  # someone created a label on GitHub meanwhile
    with pytest.raises(LabelPlanOutdatedError, match="preview"):
        await labels.apply(REPO, "setup", plan.id)
    assert github.writes == []


async def test_preview_and_apply_need_a_watched_repo(labels: LabelSetupService) -> None:
    with pytest.raises(RepoNotWatchedError):
        await labels.preview(REPO, "setup")
    with pytest.raises(RepoNotWatchedError):
        await labels.planner.current(REPO, "migrate")


async def test_concurrent_applies_on_one_repo_run_one_at_a_time(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    await synced(github, store, clock)
    first = await labels.preview(REPO, "setup")
    second = await labels.preview(REPO, "setup")
    results = await asyncio.gather(
        labels.apply(REPO, "setup", first.id),
        labels.apply(REPO, "setup", second.id),
        return_exceptions=True,
    )
    assert not isinstance(results[0], BaseException) and results[0].complete
    # The second sees the labels the first created: what it previewed no longer stands.
    assert isinstance(results[1], LabelPlanOutdatedError)
    created = [args for name, args in github.writes if name == "create_label"]
    assert len(created) == len(set(created)) == len(first.ops)


# Plan ids -----------------------------------------------------------------------------


async def test_migration_requires_preview_plan_id(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "bug")
    github.put(issue(1, labels=("design needed", "bug")))
    await synced(github, store, clock)

    with pytest.raises(LabelPlanNotFoundError):
        await labels.apply(REPO, "migrate", "never-previewed")
    plan = await labels.preview(REPO, "migrate")
    with pytest.raises(LabelPlanNotFoundError):
        await labels.apply("acme/other", "migrate", plan.id)  # another repo's id
    with pytest.raises(LabelPlanKindMismatchError):
        await labels.apply(REPO, "setup", plan.id)
    # Every apply consumes its plan: the next needs a fresh preview.
    with pytest.raises(LabelPlanNotFoundError):
        await labels.apply(REPO, "migrate", plan.id)

    expired = await labels.preview(REPO, "migrate")
    clock.now += PLAN_TTL + timedelta(seconds=1)
    with pytest.raises(LabelPlanExpiredError):
        await labels.apply(REPO, "migrate", expired.id)
    assert github.writes == []


async def test_old_plans_are_pruned_when_a_new_one_is_saved(
    labels: LabelSetupService,
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    conn: sqlite3.Connection,
) -> None:
    await synced(github, store, clock)
    await labels.preview(REPO, "setup")
    clock.now += PLAN_TTL * 2
    await labels.preview(REPO, "setup")
    assert conn.execute("SELECT count(*) FROM label_plans").fetchone() == (1,)


# Migration ----------------------------------------------------------------------------


async def test_migration_relabels_then_deletes(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "apple", "platform:ios", "area:apple", "bug")
    github.put(
        issue(1, labels=("design needed", "bug")),
        issue(2, labels=("apple", "platform:ios")),
        issue(3, labels=("bug",)),
    )
    await synced(github, store, clock)

    plan = await labels.preview(REPO, "migrate")
    assert [type(op) for op in plan.ops] == [CreateLabel] + [DeleteLabel] * 3
    assert [op.members for op in plan.ops if isinstance(op, DeleteLabel)] == [(1,), (2,), (2,)]
    run = await labels.apply(REPO, "migrate", plan.id)

    assert run.complete and run.note is None
    assert github.writes == [
        ("create_label", ("design-needed",)),
        ("add_labels", (1, ("design-needed",))),
        ("remove_label", (1, "design needed")),
        ("add_labels", (2, ("area:apple",))),
        ("remove_label", (2, "platform:ios")),
        ("remove_label", (2, "apple")),  # already has area:apple: nothing to add
        ("delete_label", ("design needed",)),
        ("delete_label", ("platform:ios",)),
        ("delete_label", ("apple",)),
    ]
    assert github.issues[(REPO, 1)].labels == ("bug", "design-needed")
    assert github.issues[(REPO, 2)].labels == ("area:apple",)
    state = store.repo(REPO)
    assert state is not None and state.migrated


async def test_migration_relabels_an_issue_given_an_alias_after_the_last_sync(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "design-needed")
    github.put(issue(1, labels=("design needed",)))
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    # Labelled on GitHub after the sync and after the preview: never in the cache.
    github.put(issue(9, labels=("design needed", "bug")))
    assert store.issue(REPO, 9) is None

    run = await labels.apply(REPO, "migrate", plan.id)

    assert run.complete
    assert github.issues[(REPO, 9)].labels == ("bug", "design-needed")  # kept its meaning
    assert ("delete_label", ("design needed",)) in github.writes


async def test_migration_relabels_pull_requests_carrying_an_alias(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("apple", "area:apple")
    github.prs[5] = ("apple", "review:bot")
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    assert plan.ops == (DeleteLabel("apple", "area:apple", (5,)),)
    run = await labels.apply(REPO, "migrate", plan.id)
    assert run.complete
    assert github.prs[5] == ("review:bot", "area:apple")


async def test_delete_is_skipped_when_a_live_member_relabel_fails(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "apple", "area:apple", "design-needed")
    github.put(issue(1, labels=("design needed",)), issue(2, labels=("apple",)))
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    github.put(issue(3, labels=("design needed",)))  # after the sync
    github.fail["add_labels:3"] = GitHubRequestError(403, "Resource not accessible")

    run = await labels.apply(REPO, "migrate", plan.id)

    states = [(r.op.name, r.state) for r in run.results]
    assert states == [("design needed", "failed"), ("apple", "skipped")]
    failed = run.results[0]
    assert failed.failed_issues == (3,)
    assert "#3" in str(failed.cause) and "Resource not accessible" in str(failed.cause)
    assert "not deleted" in str(failed.cause)
    assert not run.complete
    assert run.note == "Nothing was deleted: 1 step failed, so the old labels were kept."
    assert run.results[1].cause == run.note
    assert not any(name == "delete_label" for name, _ in github.writes)
    # Every other member was still moved; only the delete waited.
    assert github.issues[(REPO, 1)].labels == ("design-needed",)
    assert github.issues[(REPO, 2)].labels == ("area:apple",)
    state = store.repo(REPO)
    assert state is not None and not state.migrated


async def test_a_move_uses_githubs_current_spelling(github: FakeGitHub) -> None:
    github.set_labels("Design Needed", "design-needed")
    github.put(issue(1, labels=("Design Needed", "bug")))
    run = await executor(github).run(REPO, [DeleteLabel("design needed", "design-needed")])
    assert run.complete
    assert github.writes == [
        ("add_labels", (1, ("design-needed",))),
        ("remove_label", (1, "Design Needed")),
        ("delete_label", ("design needed",)),
    ]
    assert run.relabelled == {1: ("bug", "design-needed")}


async def test_a_move_never_adds_a_label_missing_on_github(github: FakeGitHub) -> None:
    github.set_labels("design needed")
    github.put(issue(1, labels=("design needed",)))
    github.fail["create_label:design-needed"] = GitHubRequestError(422, "Validation Failed")
    run = await executor(github).run(
        REPO,
        [
            CreateLabel("design-needed", "0e8a16", "Readiness"),
            DeleteLabel("design needed", "design-needed"),
        ],
    )
    assert [r.state for r in run.results] == ["failed", "failed"]
    assert "Validation Failed" in str(run.results[0].cause)
    assert "never creates labels implicitly" in str(run.results[1].cause)
    assert [name for name, _ in github.writes] == ["create_label"]


async def test_a_label_name_githubs_filter_cannot_list_is_never_deleted(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.files[(REPO, LABELS_YML)] = (
        'priority:\n  - name: "priority: high"\n    aliases: ["p, high"]\n'
    )
    github.set_labels("p, high", "priority: high")
    github.put(issue(1, labels=("p, high",)))
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    assert plan.ops == (DeleteLabel("p, high", "priority: high", ()),)
    assert "labeled" not in github.call_names()  # never listed by a filter that can't say it

    run = await labels.apply(REPO, "migrate", plan.id)

    assert [r.state for r in run.results] == ["skipped"]
    assert "comma" in str(run.results[0].cause) and "by hand" in str(run.results[0].cause)
    assert github.writes == [] and not run.complete
    with pytest.raises(InvalidLabelError):
        validate_label_filter("p, high")


@pytest.mark.parametrize("old", ["priority: high", "🚀 launch", "a/b", "%2e", "100%"])
async def test_odd_label_names_move_and_delete(github: FakeGitHub, old: str) -> None:
    github.set_labels(old, "area:new")
    github.put(issue(1, labels=(old,)))
    run = await executor(github).run(REPO, [DeleteLabel(old, "area:new")])
    assert run.complete
    assert github.writes == [
        ("add_labels", (1, ("area:new",))),
        ("remove_label", (1, old)),
        ("delete_label", (old,)),
    ]


async def test_deleting_a_label_already_gone_is_done(github: FakeGitHub) -> None:
    github.fail["delete_label:old"] = GitHubRequestError(404, "Not Found")
    github.fail["delete_label:locked"] = GitHubRequestError(403, "Forbidden")
    run = await executor(github).run(REPO, [DeleteLabel("old"), DeleteLabel("locked")])
    assert [(r.state, r.cause) for r in run.results] == [
        ("done", "already gone"),
        ("failed", "GitHub answered 403: Forbidden"),
    ]


async def test_a_failed_live_listing_fails_that_delete(github: FakeGitHub) -> None:
    github.set_labels("old", "new")
    github.fail["labeled:old"] = GitHubRequestError(500, "boom")
    run = await executor(github).run(REPO, [DeleteLabel("old", "new")])
    assert [r.state for r in run.results] == ["failed"] and "boom" in str(run.results[0].cause)
    assert github.writes == []


# Rate limits, outages and pacing (IR5) ------------------------------------------------


async def test_a_rate_limit_stops_the_batch_and_records_reset_at(
    labels: LabelSetupService,
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    back_off: BackOff,
) -> None:
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "setup")
    assert len(plan.ops) > 3
    second = plan.ops[1]
    assert isinstance(second, CreateLabel)
    github.fail[f"create_label:{second.name}"] = GitHubRateLimitedError(403, "slow down", RESET)

    run = await labels.apply(REPO, "setup", plan.id)

    assert run.rate_limited_until == RESET and run.is_rate_limited and not run.complete
    assert [r.state for r in run.results[:2]] == ["done", "failed"]
    assert {r.state for r in run.results[2:]} == {"skipped"}
    assert "rate limit" in str(run.results[2].cause)
    assert len(github.writes) == 2  # nothing after the limit
    assert back_off.calls == [RESET]


async def test_a_rate_limit_without_a_reset_time_still_stops(github: FakeGitHub) -> None:
    github.fail["delete_label:a"] = GitHubRateLimitedError(429, "slow down", None)
    run = await executor(github).run(REPO, [DeleteLabel("a"), DeleteLabel("b")])
    assert run.is_rate_limited and run.rate_limited_until is None
    assert [r.state for r in run.results] == ["failed", "skipped"]


async def test_a_rate_limit_between_add_and_remove_is_finished_by_a_new_preview(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "design-needed")
    github.put(issue(1, labels=("design needed",)))
    await synced(github, store, clock)
    github.fail["remove_label:1"] = GitHubRateLimitedError(403, "slow down", RESET)
    run = await labels.apply(REPO, "migrate", (await labels.preview(REPO, "migrate")).id)
    assert run.is_rate_limited
    assert github.issues[(REPO, 1)].labels == ("design needed", "design-needed")  # both

    del github.fail["remove_label:1"]
    github.writes.clear()
    again = await labels.apply(REPO, "migrate", (await labels.preview(REPO, "migrate")).id)
    assert again.complete
    assert github.writes == [
        ("remove_label", (1, "design needed")),
        ("delete_label", ("design needed",)),
    ]


async def test_a_rate_limit_while_planning_pauses_the_sync_too(
    labels: LabelSetupService,
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    back_off: BackOff,
) -> None:
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "setup")
    github.labels_error = GitHubRateLimitedError(403, "slow down", RESET)
    with pytest.raises(GitHubRateLimitedError):
        await labels.preview(REPO, "setup")
    with pytest.raises(GitHubRateLimitedError):
        await labels.apply(REPO, "setup", plan.id)
    assert back_off.calls == [RESET, RESET]


async def test_github_becoming_unavailable_stops_the_batch(github: FakeGitHub) -> None:
    github.fail["create_label:a"] = GitHubUnavailable("the token was rejected")
    run = await executor(github).run(
        REPO, [CreateLabel("a", "ededed", "x"), CreateLabel("b", "ededed", "x")]
    )
    assert [r.state for r in run.results] == ["failed", "skipped"]
    assert run.stopped is not None and "token was rejected" in run.stopped
    assert not run.is_rate_limited
    assert len(github.writes) == 1


async def test_writes_are_paced_and_reads_are_not(github: FakeGitHub) -> None:
    github.set_labels("old", "new")
    github.put(issue(1, labels=("old",)), issue(2, labels=("old", "new")))
    pauses = Pauses()
    run = await executor(github, pauses).run(REPO, [DeleteLabel("old", "new")])
    assert run.complete
    writes = len(github.writes)  # add 1, remove 1, remove 2, delete
    assert writes == 4 and pauses.calls == [WRITE_INTERVAL.total_seconds()] * (writes - 1)


# The cache after an apply (IR32) --------------------------------------------------------


async def test_apply_updates_the_label_cache_at_once(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "priority: high", "bug", "area:web")
    github.put(issue(1, labels=("design needed", "priority: high", "bug", "area:web")))
    await synced(github, store, clock)
    setup = await labels.apply(REPO, "setup", (await labels.preview(REPO, "setup")).id)
    assert setup.complete
    cached = {label.name for label in store.labels(REPO)}
    assert {"epic", "initiative", "documentation"} <= cached

    run = await labels.apply(REPO, "migrate", (await labels.preview(REPO, "migrate")).id)
    assert run.complete
    assert "design needed" not in {label.name for label in store.labels(REPO)}
    stored = store.cached_issue(REPO, 1)
    assert stored is not None
    assert stored.issue.labels == ("priority: high", "bug", "area:web", "design-needed")
    assert stored.triage.readiness == "design-needed" and stored.triage.complete


async def test_a_failing_cache_update_never_fails_the_apply(
    labels: LabelSetupService,
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "setup")

    def broken(repo: str, labels: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "replace_labels", broken)
    with caplog.at_level(logging.INFO):
        run = await labels.apply(REPO, "setup", plan.id)
    assert run.complete
    assert "database is locked" in caplog.text  # the cause is kept, not swallowed
    assert f"plan={plan.id}" in caplog.text and "done=" in caplog.text


async def test_a_rate_limit_while_listing_members_stops_the_batch(github: FakeGitHub) -> None:
    github.set_labels("old", "new")
    github.fail["labeled:old"] = GitHubRateLimitedError(403, "slow down", RESET)
    run = await executor(github).run(REPO, [DeleteLabel("old", "new"), DeleteLabel("x")])
    assert run.is_rate_limited and [r.state for r in run.results] == ["failed", "skipped"]


async def test_a_rate_limit_without_a_listener_is_still_reported(
    github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    service = LabelSetupService(github, store, clock, pause=Pauses())
    await synced(github, store, clock)
    plan = await service.preview(REPO, "setup")
    github.fail[f"create_label:{plan.ops[0].name}"] = GitHubRateLimitedError(403, "slow", None)
    assert (await service.apply(REPO, "setup", plan.id)).is_rate_limited


async def test_an_apply_that_breaks_mid_batch_is_still_logged(
    labels: LabelSetupService,
    github: FakeGitHub,
    store: SqliteIssuesStore,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github.set_labels("design needed", "design-needed")
    github.put(issue(1, labels=("design needed",)))
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    github.fail["add_labels:1"] = RuntimeError("bug in a double")
    with pytest.raises(RuntimeError):
        await labels.apply(REPO, "migrate", plan.id)
    assert f"plan={plan.id} did not finish" in caplog.text


# The recheck right before each delete (IR31, re-review N1) ------------------------------


async def test_a_member_labelled_during_the_batch_is_moved_before_the_delete(
    github: FakeGitHub,
) -> None:
    github.set_labels("old", "new", "gone")
    github.put(issue(1, labels=("old",)))

    def label_late(name: str, count: int) -> None:
        if name == "old" and count == 2:  # after the first pass moved #1
            github.put(issue(7, labels=("old", "bug")))
            github.prs[8] = ("old",)

    github.before_labeled = label_late
    run = await executor(github).run(REPO, [DeleteLabel("old", "new"), DeleteLabel("gone", "new")])
    assert run.complete
    assert github.issues[(REPO, 7)].labels == ("bug", "new")
    assert github.prs[8] == ("new",)
    writes = [(name, args) for name, args in github.writes]
    assert writes.index(("remove_label", (7, "old"))) < writes.index(("delete_label", ("old",)))
    assert writes.index(("remove_label", (8, "old"))) < writes.index(("delete_label", ("old",)))


async def test_a_failed_recheck_move_blocks_that_delete_and_later_ones(
    github: FakeGitHub,
) -> None:
    github.set_labels("old", "older", "new")
    github.put(issue(1, labels=("old",)), issue(2, labels=("older",)))

    def label_late(name: str, count: int) -> None:
        if name == "old" and count == 2:
            github.put(issue(7, labels=("old",)))

    github.before_labeled = label_late
    github.fail["add_labels:7"] = GitHubRequestError(403, "Resource not accessible")
    run = await executor(github).run(REPO, [DeleteLabel("old", "new"), DeleteLabel("older", "new")])
    assert [r.state for r in run.results] == ["failed", "skipped"]
    assert run.results[0].failed_issues == (7,)
    assert run.results[1].cause == run.note and run.note is not None
    assert not any(name == "delete_label" for name, _ in github.writes)
    # The first pass still moved the members it found.
    assert github.issues[(REPO, 2)].labels == ("new",)


async def test_the_apply_shape_check_lists_no_members(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "apple", "area:apple", "design-needed")
    github.put(issue(1, labels=("design needed",)))
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    assert github.call_names().count("labeled") == 2  # the preview lists each alias once
    github.calls.clear()
    run = await labels.apply(REPO, "migrate", plan.id)
    assert run.complete
    # Only the executor's two live listings per alias: the re-plan lists none.
    assert github.call_names().count("labeled") == 4


async def test_a_comma_alias_previews_as_a_delete_that_will_be_skipped(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.files[(REPO, LABELS_YML)] = (
        'priority:\n  - name: "priority: high"\n    aliases: ["p, high"]\n'
    )
    github.set_labels("p, high", "priority: high")
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "migrate")
    assert [skip_cause(op) for op in plan.ops] == [COMMA_CAUSE]


async def test_executor_refuses_a_move_onto_the_same_label(github: FakeGitHub) -> None:
    nfc = unicodedata.normalize("NFC", "area:café")
    nfd = unicodedata.normalize("NFD", "area:café")
    github.set_labels(nfc)
    github.put(issue(1, labels=(nfc,)))
    run = await executor(github).run(REPO, [DeleteLabel(nfd, nfc), DeleteLabel("x")])
    assert [r.state for r in run.results] == ["failed", "skipped"]
    assert "same label" in str(run.results[0].cause)
    assert github.writes == [] and "labeled" not in github.call_names()


async def test_cache_update_after_apply_hits_rows_of_a_differently_cased_repo(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "design-needed")
    github.put(issue(1, labels=("design needed",)))
    await synced(github, store, clock)
    plan = await labels.preview("ACME/WEB", "migrate")  # the author's spelling
    assert (await labels.apply("Acme/Web", "migrate", plan.id)).complete
    cached = store.cached_issue(REPO, 1)
    assert cached is not None and cached.issue.labels == ("design-needed",)


async def test_setup_and_migration_refuse_a_broken_labels_yml(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.files[(REPO, LABELS_YML)] = "readiness: [unclosed\n"
    await synced(github, store, clock)
    with pytest.raises(SchemaHasErrorsError, match="not valid YAML"):
        await labels.preview(REPO, "setup")
    with pytest.raises(SchemaHasErrorsError, match="not valid YAML"):
        await labels.preview(REPO, "migrate")
    assert github.writes == []


async def test_a_labels_yml_changed_after_the_preview_is_a_409_with_no_writes(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "setup")
    # The file changes on GitHub; the cache (no sync since) still holds the old schema.
    github.files[(REPO, LABELS_YML)] = AMICUS_YML + "  - name: area:web\n"
    with pytest.raises(LabelPlanOutdatedError):
        await labels.apply(REPO, "setup", plan.id)
    assert github.writes == []


async def test_a_labels_yml_that_cannot_be_read_applies_nothing(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    await synced(github, store, clock)
    plan = await labels.preview(REPO, "setup")
    github.file_error = GitHubRequestError(502, "Bad Gateway")
    with pytest.raises(GitHubRequestError):
        await labels.apply(REPO, "setup", plan.id)
    assert github.writes == []


async def test_the_cache_after_a_migration_is_triaged_with_the_fresh_schema(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.files[(REPO, LABELS_YML)] = "readiness:\n  - human-needed\n"
    github.set_labels("agent-ready", "ready", "priority: high", "bug", "area:web")
    complete = ("agent-ready", "priority: high", "bug", "area:web")
    github.put(issue(1, labels=complete, created=clock.now + timedelta(seconds=30)))
    await synced(github, store, clock)
    inbox = IssueQueries(store, clock)
    assert [r.number for r in inbox.view("inbox", IssueFilters()).rows] == [1]
    # labels.yml now names ``ready``, with ``agent-ready`` as its alias; no sync since.
    github.files[(REPO, LABELS_YML)] = (
        "readiness:\n  - name: ready\n    aliases: [agent-ready]\n  - human-needed\n"
    )
    plan = await labels.preview(REPO, "migrate")
    assert plan.ops == (DeleteLabel("agent-ready", "ready", (1,)),)
    assert (await labels.apply(REPO, "migrate", plan.id)).complete
    cached = store.cached_issue(REPO, 1)
    assert cached is not None and "ready" in cached.issue.labels
    assert cached.triage.complete and cached.triage.readiness == "ready"
    assert inbox.view("inbox", IssueFilters()).rows == []
