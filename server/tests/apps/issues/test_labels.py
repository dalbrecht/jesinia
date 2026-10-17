# SPDX-License-Identifier: AGPL-3.0-or-later
"""The label setup and migration batches (spec §2, §6.2): previewed, then applied with an
approval through the ordered ``LabelOp`` executor. Labels are read fresh before anything is
created or relabelled (IR4), a rate limit stops the batch (IR5), and old labels are deleted only
after every relabel succeeded (IR12)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jesinia.apps.issues.application.labels import (
    LabelExecutor,
    LabelSetupService,
)
from jesinia.apps.issues.application.sync import LABELS_YML, SyncService
from jesinia.apps.issues.domain.labels import (
    PLAN_TTL,
    CreateLabel,
    DeleteLabel,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanNotFoundError,
    LabelPlanOutdatedError,
    Relabel,
    RepoNotSyncedError,
)
from jesinia.apps.issues.domain.triage import RepoNotWatchedError
from jesinia.apps.issues.infrastructure.store import SqliteIssuesStore
from jesinia.apps.issues.module import ISSUES_DB
from jesinia.core.db import open_db
from jesinia.services.github.application.service import (
    GitHubRateLimitedError,
    GitHubRequestError,
)
from jesinia.services.github.domain.issue import Issue, Label
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
    """``FakeIssues`` plus the label writes, applied to the in-memory repo; ``fail`` maps a
    write (``"create_label:name"``, ``"add_labels:n"``, ...) to the error it raises."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, tuple[object, ...]]] = []
        self.fail: dict[str, Exception] = {}
        self.labels_error: Exception | None = None

    def label_names(self) -> list[str]:
        return [label.name for label in self.repo_labels.get(REPO, [])]

    def set_labels(self, *names: str) -> None:
        self.repo_labels[REPO] = [Label(name, "ededed", "") for name in names]

    def _write(self, name: str, key: object, *args: object) -> None:
        self.writes.append((name, args))
        error = self.fail.get(f"{name}:{key}")
        if error is not None:
            raise error

    async def labels(self, repo: str) -> list[Label]:
        if self.labels_error is not None:
            raise self.labels_error
        return await super().labels(repo)

    async def get(self, repo: str, n: int) -> Issue:
        self.calls.append(("get", (repo, n)))
        return self.issues[(repo, n)]

    async def add_labels(self, repo: str, n: int, names: Sequence[str]) -> tuple[str, ...]:
        self._write("add_labels", n, n, tuple(names))
        current = self.issues[(repo, n)]
        labels = (*current.labels, *names)
        self.issues[(repo, n)] = replace(current, labels=labels)
        return labels

    async def remove_label(self, repo: str, n: int, name: str) -> tuple[str, ...]:
        self._write("remove_label", n, n, name)
        current = self.issues[(repo, n)]
        labels = tuple(label for label in current.labels if label != name)
        self.issues[(repo, n)] = replace(current, labels=labels)
        return labels

    async def create_label(self, repo: str, name: str, color: str, description: str) -> Label:
        self._write("create_label", name, name)
        label = Label(name, color, description)
        self.repo_labels.setdefault(repo, []).append(label)
        return label

    async def delete_label(self, repo: str, name: str) -> None:
        self._write("delete_label", name, name)
        self.repo_labels[repo] = [lab for lab in self.repo_labels[repo] if lab.name != name]


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
def labels(
    github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock, back_off: BackOff
) -> LabelSetupService:
    return LabelSetupService(github, store, clock, on_rate_limit=back_off)


async def synced(github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock) -> None:
    sync = SyncService(github, store, clock)
    await sync.watch(REPO)
    clock.now += timedelta(minutes=1)
    await sync.sync(REPO)
    github.calls.clear()


# Setup --------------------------------------------------------------------------------


async def test_setup_creates_only_missing_canonical_labels(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    present = [
        "agent-ready",
        "human-needed",
        "information-needed",
        "design needed",
        "blocked",
        "icebox",
        "priority: high",
        "priority: medium",
        "priority: low",
        "bug",
        "enhancement",
        "tech-debt",
        "test",
        "performance",
        "security",
        "apple",
    ]
    github.set_labels(*present)
    await synced(github, store, clock)

    plan = await labels.preview(REPO, "setup")
    assert [op.name for op in plan.ops if isinstance(op, CreateLabel)] == [
        "documentation",
        "epic",
        "initiative",
    ]
    assert len(plan.ops) == 3 and plan.expires_at == clock.now + PLAN_TTL
    assert github.writes == []  # a preview never writes

    github.calls.clear()
    run = await labels.apply(REPO, "setup", plan.id)
    assert run.complete and [r.state for r in run.results] == ["done"] * 3
    assert [w for w, _ in github.writes] == ["create_label"] * 3
    # IR4: the labels were read fresh before anything was created.
    assert github.call_names()[0] == "labels"
    assert (await labels.planner.setup(REPO)) == []


async def test_setup_skips_a_label_that_exists_by_the_time_it_runs(github: FakeGitHub) -> None:
    github.set_labels("bug")
    run = await LabelExecutor(github).run(
        REPO, [CreateLabel("Bug", "1d76db", "Type"), CreateLabel("epic", "5319e7", "H")]
    )
    assert run.complete and [r.cause for r in run.results] == ["already on GitHub", None]
    assert github.writes == [("create_label", ("epic",))]


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
        await labels.planner.migrate(REPO)


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


async def test_migration_needs_a_finished_sync(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    await SyncService(github, store, clock).watch(REPO)
    with pytest.raises(RepoNotSyncedError):
        await labels.preview(REPO, "migrate")


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
    assert [type(op) for op in plan.ops] == [CreateLabel, Relabel, Relabel] + [DeleteLabel] * 3
    run = await labels.apply(REPO, "migrate", plan.id)

    assert run.complete and run.note is None
    assert github.writes == [
        ("create_label", ("design-needed",)),
        ("add_labels", (1, ("design-needed",))),
        ("remove_label", (1, "design needed")),
        ("add_labels", (2, ("area:apple",))),
        ("remove_label", (2, "apple")),
        ("remove_label", (2, "platform:ios")),
        ("delete_label", ("design needed",)),
        ("delete_label", ("platform:ios",)),
        ("delete_label", ("apple",)),
    ]
    assert github.issues[(REPO, 1)].labels == ("bug", "design-needed")
    assert github.issues[(REPO, 2)].labels == ("area:apple",)
    state = store.repo(REPO)
    assert state is not None and state.migrated


async def test_migration_keeps_old_labels_when_any_relabel_fails(
    labels: LabelSetupService, github: FakeGitHub, store: SqliteIssuesStore, clock: FakeClock
) -> None:
    github.set_labels("design needed", "apple", "area:apple", "design-needed")
    github.put(issue(1, labels=("design needed",)), issue(2, labels=("apple",)))
    await synced(github, store, clock)
    github.fail["add_labels:1"] = GitHubRequestError(403, "Resource not accessible")

    run = await labels.apply(REPO, "migrate", (await labels.preview(REPO, "migrate")).id)

    states = [(type(r.op).__name__, r.state) for r in run.results]
    assert states == [
        ("Relabel", "failed"),
        ("Relabel", "done"),
        ("DeleteLabel", "skipped"),
        ("DeleteLabel", "skipped"),
    ]
    assert "Resource not accessible" in str(run.results[0].cause)
    assert not run.complete
    assert run.note == "Nothing was deleted: 1 step failed, so the old labels were kept."
    assert all(r.cause == run.note for r in run.results[2:])
    assert not any(name == "delete_label" for name, _ in github.writes)
    state = store.repo(REPO)
    assert state is not None and not state.migrated


async def test_relabel_works_from_a_fresh_read_of_the_issue(github: FakeGitHub) -> None:
    github.set_labels("design needed", "design-needed")
    github.put(issue(1, labels=("Design Needed", "bug")), issue(2, labels=("bug",)))
    run = await LabelExecutor(github).run(
        REPO,
        [
            Relabel(1, (("design needed", "design-needed"),)),
            Relabel(2, (("design needed", "design-needed"),)),  # removed on GitHub since
        ],
    )
    assert run.complete
    assert run.results[1].cause == "nothing to relabel: the old labels are gone"
    # GitHub's current spelling is what gets removed.
    assert github.writes == [
        ("add_labels", (1, ("design-needed",))),
        ("remove_label", (1, "Design Needed")),
    ]


async def test_relabel_never_adds_a_label_missing_on_github(github: FakeGitHub) -> None:
    github.set_labels("design needed")
    github.put(issue(1, labels=("design needed",)))
    github.fail["create_label:design-needed"] = GitHubRequestError(422, "Validation Failed")
    run = await LabelExecutor(github).run(
        REPO,
        [
            CreateLabel("design-needed", "0e8a16", "Readiness"),
            Relabel(1, (("design needed", "design-needed"),)),
            DeleteLabel("design needed"),
        ],
    )
    assert [r.state for r in run.results] == ["failed", "failed", "skipped"]
    assert "Validation Failed" in str(run.results[0].cause)
    assert "never creates labels implicitly" in str(run.results[1].cause)
    assert [name for name, _ in github.writes] == ["create_label"]


async def test_deleting_a_label_already_gone_is_done(github: FakeGitHub) -> None:
    github.fail["delete_label:old"] = GitHubRequestError(404, "Not Found")
    github.fail["delete_label:locked"] = GitHubRequestError(403, "Forbidden")
    run = await LabelExecutor(github).run(REPO, [DeleteLabel("old"), DeleteLabel("locked")])
    assert [(r.state, r.cause) for r in run.results] == [
        ("done", "already gone"),
        ("failed", "GitHub answered 403: Forbidden"),
    ]


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
    run = await LabelExecutor(github).run(REPO, [DeleteLabel("a"), DeleteLabel("b")])
    assert run.is_rate_limited and run.rate_limited_until is None
    assert [r.state for r in run.results] == ["failed", "skipped"]


async def test_relabel_of_an_issue_that_already_has_the_canonical_only_removes(
    github: FakeGitHub,
) -> None:
    github.set_labels("design needed", "design-needed")
    github.put(issue(1, labels=("design needed", "design-needed")))
    run = await LabelExecutor(github).run(REPO, [Relabel(1, (("design needed", "design-needed"),))])
    assert run.complete
    assert github.writes == [("remove_label", (1, "design needed"))]
