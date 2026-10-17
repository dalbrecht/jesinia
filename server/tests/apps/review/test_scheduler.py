# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pre-review scheduler (priority, concurrency, gates, setting) and the prune job."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from jesinia.apps.review.application.ports import KeepProvider
from jesinia.apps.review.application.queue import QueueService
from jesinia.apps.review.application.scheduler import (
    MAX_INTERRUPTIONS,
    PreReviewRequest,
    Scheduler,
    SchedulerConfigError,
    WorkspacePruner,
)
from jesinia.apps.review.application.sessions import ReviewSessionRegistry
from jesinia.apps.review.domain.gates import DEBOUNCE
from jesinia.apps.review.domain.queue import PrRef, QueueFilter, RepoConfig
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.module import PRUNE_JOB, REVIEW_DB, SYNC_JOB, ReviewModule
from jesinia.core.config import Config
from jesinia.core.db import open_db
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Scheduler as CoreScheduler
from jesinia.services.github.application.service import GitHubService
from tests.apps.review.fakes import BASE, HEAD, START, FakeClock, FakePullRequests, pull

HEAD_B = "c" * 40
HEAD_C = "d" * 40


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, REVIEW_DB)
    yield connection
    connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SqliteReviewStore:
    store = SqliteReviewStore(conn)
    store.upsert_repo(RepoConfig("acme/web", watched=True))
    return store


@pytest.fixture
def github() -> FakePullRequests:
    return FakePullRequests()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock) -> QueueService:
    return QueueService(github, store, clock)


class Runner:
    """Records each pre-review; each one waits for its PR number's release."""

    def __init__(self, *, block: bool = True) -> None:
        self.started: list[PreReviewRequest] = []
        self.releases: dict[int, asyncio.Event] = {}
        self.block = block
        self.in_flight = 0
        self.peak = 0
        self.fail_with: Exception | None = None

    def release(self, number: int) -> None:
        self.releases.setdefault(number, asyncio.Event()).set()

    async def __call__(self, request: PreReviewRequest) -> None:
        self.started.append(request)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.block:
                await self.releases.setdefault(request.ref.number, asyncio.Event()).wait()
            if self.fail_with is not None:
                raise self.fail_with
        finally:
            self.in_flight -= 1

    @property
    def numbers(self) -> list[int]:
        return [request.ref.number for request in self.started]


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def states(queue: QueueService) -> dict[int, str]:
    view = await queue.list(QueueFilter(include_dismissed=True))
    return {row.number: row.prereview_state for row in view.rows}


async def test_scheduler_priority_and_concurrency_two(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    oldest = clock.now - timedelta(days=9)
    github.inbox = [
        pull(1, created=oldest),  # the rest, though oldest
        pull(2, author="dependabot[bot]", bot=True),  # agent-authored
        pull(3, requested=True),  # review requested
    ]
    github.others = {("acme/web", 4): pull(4)}
    await queue.sync()
    await queue.load("acme/web#4")
    clock.advance(DEBOUNCE.total_seconds() / 60)
    runner = Runner()
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()
    await until(lambda: len(runner.started) == 2)
    await scheduler.tick()  # a second tick while both slots are busy starts nothing
    for _ in range(10):
        await asyncio.sleep(0)

    assert runner.numbers == [4, 3]
    assert (await states(queue)) == {4: "running", 3: "running", 2: "queued", 1: "queued"}
    runner.release(4)
    await until(lambda: len(runner.started) == 3)
    assert runner.numbers == [4, 3, 2]
    runner.release(3)
    await until(lambda: len(runner.started) == 4)
    runner.release(2)
    runner.release(1)
    await scheduler.drain()

    assert runner.numbers == [4, 3, 2, 1] and runner.peak == 2
    assert set((await states(queue)).values()) == {"ready"}
    first = runner.started[0]
    assert (first.head_sha, first.base_sha, first.mode, first.loaded) == (
        HEAD,
        BASE,
        "triage",
        True,
    )
    assert first.depth == "quick" and first.display_repo == "acme/web"


async def test_rapid_pushes_cost_one_review(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    runner = Runner(block=False)
    scheduler = Scheduler(store, clock, run=runner)
    for head in (HEAD, HEAD_B, HEAD_C):  # an agent pushes three times, 3 minutes apart
        github.inbox = [pull(1, head=head, author="codex[bot]", bot=True)]
        await queue.sync()
        await scheduler.tick()
        clock.advance(3)
    for _ in range(2):  # 3, then 7 minutes after the last push: still settling
        await queue.sync()
        await scheduler.tick()
        assert runner.started == [] and (await states(queue)) == {1: "debouncing"}
        clock.advance(4)

    await queue.sync()  # 11 quiet minutes after the last push
    await scheduler.tick()
    await scheduler.drain()
    clock.advance(30)
    await queue.sync()
    await scheduler.tick()
    await scheduler.drain()

    assert [request.head_sha for request in runner.started] == [HEAD_C]


async def test_the_ci_gate_holds_back_pre_reviews(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1, head=HEAD), pull(2, head=HEAD_B), pull(3, head=HEAD_C)]
    github.ci = {HEAD: "pending", HEAD_B: "failing", HEAD_C: "none"}
    await queue.sync()
    await queue.load("acme/web#1")  # loaded PRs still respect the CI gate
    clock.advance(5)  # within the 10 minutes a head without checks waits for CI
    runner = Runner(block=False)
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()

    assert runner.started == []
    assert (await states(queue)) == {1: "waiting_ci", 2: "ci_failing", 3: "waiting_ci"}

    clock.advance(5)  # still no checks on PR 3 ten minutes on: it has no CI (R28)
    await scheduler.tick()
    await scheduler.drain()
    assert runner.numbers == [3]


async def test_pre_review_setting_and_per_repo_override(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    store.set_prereview_default("on_open")
    store.upsert_repo(RepoConfig("acme/api", watched=True, prereview="everything"))
    github.inbox = [pull(1), pull(2), pull(3, "acme/api")]
    await queue.sync()
    await queue.load("acme/web#2")
    clock.advance(11)
    runner = Runner(block=False)
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()
    await scheduler.drain()

    assert sorted((r.ref.number, r.mode, r.loaded) for r in runner.started) == [
        (2, "on_open", True),
        (3, "everything", False),
    ]
    view = await queue.list(QueueFilter())
    waiting = next(row for row in view.rows if row.number == 1)
    assert waiting.prereview_state == "not_started" and "open" in waiting.prereview_reason
    assert store.prereview_default() == "on_open"


async def test_a_failed_pre_review_shows_its_cause_and_is_not_retried(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    clock.advance(11)
    runner = Runner(block=False)
    runner.fail_with = RuntimeError("the agent session crashed")
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()
    await scheduler.drain()
    await scheduler.tick()
    await scheduler.drain()

    assert len(runner.started) == 1
    (row,) = (await queue.list(QueueFilter())).rows
    assert row.prereview_state == "failed" and "the agent session crashed" in row.prereview_reason


async def test_fetch_error_rows_are_never_scheduled(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    github.inbox = [pull(1, fetch_error="GitHub answered 502: oops"), pull(2, fetch_error="no")]
    await queue.sync()
    await queue.load("acme/web#1")
    clock.advance(60)
    runner = Runner(block=False)
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()
    await scheduler.drain()

    assert runner.started == []
    assert (await states(queue)) == {1: "fetch_failed", 2: "fetch_failed"}


async def test_without_a_runner_gates_are_computed_but_nothing_runs(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1), pull(2, head=HEAD_B)]
    github.ci = {HEAD_B: "pending"}
    await queue.sync()
    clock.advance(11)
    scheduler = Scheduler(store, clock)

    await scheduler.tick()
    await scheduler.drain()

    assert (await states(queue)) == {1: "queued", 2: "waiting_ci"}
    assert store.rounds_for_heads() == {}


async def test_a_head_change_mid_run_leaves_the_new_head_to_its_own_gate(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    clock.advance(11)
    runner = Runner()
    scheduler = Scheduler(store, clock, run=runner)
    await scheduler.tick()
    await until(lambda: len(runner.started) == 1)

    github.inbox = [pull(1, head=HEAD_B)]
    await queue.sync()
    await scheduler.tick()  # the run for the old head is still going: nothing new starts
    runner.release(1)
    await scheduler.drain()

    assert (await states(queue)) == {1: "debouncing"}
    clock.advance(11)
    await scheduler.tick()
    await scheduler.drain()
    assert [request.head_sha for request in runner.started] == [HEAD, HEAD_B]


async def test_a_pre_review_cut_short_by_a_stop_runs_again(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    clock.advance(11)
    runner = Runner()
    scheduler = Scheduler(store, clock, run=runner)
    await scheduler.tick()
    await until(lambda: len(runner.started) == 1)

    await scheduler.stop()
    assert (await states(queue)) == {1: "queued"}
    restarted = Scheduler(store, clock, run=runner)
    assert restarted.recover() == 0  # the stop already recorded the interruption
    await restarted.tick()
    await until(lambda: len(runner.started) == 2)
    runner.release(1)
    await restarted.drain()

    assert (await states(queue)) == {1: "ready"}
    assert [r.head_sha for r in runner.started] == [HEAD, HEAD]
    # R51: the interrupted round is resumed, never replaced.
    assert runner.started[0].round_id == runner.started[1].round_id


async def test_a_crash_mid_review_is_retried_until_three_interruptions(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    clock.advance(11)
    runner = Runner(block=False)
    for attempt in range(1, MAX_INTERRUPTIONS + 1):
        store.start_round(PrRef("acme/web", 1), HEAD, BASE, "quick", clock.now)  # then a crash
        scheduler = Scheduler(store, clock)  # no runner: watch the state only
        assert scheduler.recover() == 1
        await scheduler.tick()
        expected = "queued" if attempt < MAX_INTERRUPTIONS else "failed"
        assert (await states(queue)) == {1: expected}

    scheduler = Scheduler(store, clock, run=runner)
    await scheduler.tick()
    await scheduler.drain()

    assert runner.started == []
    (row,) = (await queue.list(QueueFilter())).rows
    assert "interrupted 3 times" in row.prereview_reason
    assert "Jesinia stopped" in row.prereview_reason  # the original cause is kept


async def test_a_round_opened_after_the_tick_is_not_duplicated(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1, requested=True), pull(2)]
    await queue.sync()
    clock.advance(11)
    runner = Runner()
    scheduler = Scheduler(store, clock, run=runner, concurrency=1)
    await scheduler.tick()
    await until(lambda: len(runner.started) == 1)

    store.start_round(PrRef("acme/web", 2), HEAD, BASE, "deep", clock.now)  # the user opened it
    runner.release(1)
    await scheduler.drain()

    assert runner.numbers == [1]


async def test_a_tick_that_changes_nothing_writes_nothing(
    queue: QueueService,
    github: FakePullRequests,
    store: SqliteReviewStore,
    clock: FakeClock,
    conn: sqlite3.Connection,
) -> None:
    github.inbox = [pull(1), pull(2, head=HEAD_B)]
    github.ci = {HEAD_B: "pending"}
    await queue.sync()
    scheduler = Scheduler(store, clock)
    await scheduler.tick()
    before = conn.total_changes

    await scheduler.tick()

    assert conn.total_changes == before


def test_concurrency_must_be_positive(store: SqliteReviewStore, clock: FakeClock) -> None:
    with pytest.raises(SchedulerConfigError, match="concurrency"):
        Scheduler(store, clock, concurrency=0)


class FakeWorkspace:
    """Prunes the given repos in order, asking for each one's keep set as it goes."""

    def __init__(self, root: Path, repos: tuple[str, ...] = ("acme/web",)) -> None:
        self.root = root
        self.repos = repos
        self.kept: dict[str, set[str]] = {}

    async def prune(self, keep: KeepProvider) -> int:
        for repo in self.repos:
            self.kept[repo] = set(await keep(repo))
        return 3

    def tree_key(self, path: Path) -> tuple[str, str] | None:
        parts = path.relative_to(self.root).parts
        return (f"{parts[0]}/{parts[1]}", parts[3]) if len(parts) == 4 else None


async def test_prune_keeps_queued_and_in_review_heads_and_bound_trees(
    queue: QueueService,
    github: FakePullRequests,
    store: SqliteReviewStore,
    clock: FakeClock,
    tmp_path: Path,
) -> None:
    older, latest, running, done = ("1" * 40, "2" * 40), "3" * 40, "4" * 40, "6" * 40
    bound_sha = "5" * 40
    github.inbox = [pull(1), pull(2, head=HEAD_B), pull(3, "Acme/Web", head=HEAD_C)]
    await queue.sync()
    await queue.dismiss("acme/web", 2)
    one = PrRef("acme/web", 1)
    superseded = store.start_round(one, *older, "deep", clock.now)
    store.finish_round(superseded, clock.now, None, verdict="request_changes")
    reviewed = store.start_round(one, latest, BASE, "deep", clock.now)
    store.finish_round(reviewed, clock.now, None, verdict="approve")
    store.start_round(one, running, BASE, "deep", clock.now)  # still running
    posted = store.start_round(PrRef("acme/web", 3), done, BASE, "deep", clock.now)
    store.finish_round(posted, clock.now, None, verdict="approve")
    store.mark_round_done(posted)
    store.start_round(PrRef("acme/gone", 9), "7" * 40, BASE, "deep", clock.now)
    workspace = FakeWorkspace(tmp_path, ("acme/web", "acme/gone"))
    sessions = ReviewSessionRegistry()
    sessions.bind("job-1", tmp_path / "acme" / "web" / "trees" / bound_sha)
    sessions.bind("job-2", tmp_path / "stray")

    removed = await WorkspacePruner(store, workspace, sessions.bound_trees).run()

    assert removed == 3
    assert workspace.kept == {
        "acme/web": {HEAD, BASE, HEAD_C, latest, running, bound_sha},
        "acme/gone": set(),
    }


async def test_prune_skips_shas_that_are_not_full_commit_ids(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, tmp_path: Path
) -> None:
    github.inbox = [pull(1, head="abc1234")]
    await queue.sync()
    workspace = FakeWorkspace(tmp_path)

    await WorkspacePruner(store, workspace, list).run()

    assert workspace.kept == {"acme/web": {BASE}}


async def test_module_schedules_sync_and_prune(cfg: Config) -> None:
    async def token() -> str | None:
        return None

    class NoCache:
        def get(self, key: str) -> None:
            return None

        def put(self, key: str, etag: str, status: int, data: object) -> None:
            return None

        def prune(self, older_than: timedelta) -> int:
            return 0

    jobs = CoreScheduler()
    ctx = AppContext(cfg, EventBus(), jobs, services={"github": GitHubService(token, NoCache())})
    module = ReviewModule()
    await module.start(ctx)
    try:
        await jobs.run_now(SYNC_JOB)  # no token: the failure is reported, nothing raised
        view = await module.queue.list(QueueFilter())
        assert view.rows == [] and view.sync_error is not None
        assert "gh auth login" in view.sync_error
        await jobs.run_now(PRUNE_JOB)
        assert isinstance(module.scheduler, Scheduler)
    finally:
        await module.stop()


async def test_module_without_github_has_no_queue(cfg: Config) -> None:
    jobs = CoreScheduler()
    module = ReviewModule()
    await module.start(AppContext(cfg, EventBus(), jobs))
    try:
        with pytest.raises(HTTPException) as caught:
            _ = module.queue
        assert caught.value.status_code == 503 and "github" in str(caught.value.detail)
        await jobs.run_now(PRUNE_JOB)
        with pytest.raises(KeyError):
            await jobs.run_now(SYNC_JOB)
    finally:
        await module.stop()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.scheduler


async def test_a_round_running_elsewhere_is_left_alone(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    clock.advance(11)
    store.start_round(PrRef("acme/web", 1), HEAD, BASE, "deep", clock.now)  # opened by hand
    runner = Runner(block=False)
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()

    assert runner.started == [] and (await states(queue)) == {1: "debouncing"}


async def test_a_pr_dismissed_while_waiting_for_a_slot_is_not_started(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1, requested=True), pull(2)]
    await queue.sync()
    clock.advance(11)
    runner = Runner()
    scheduler = Scheduler(store, clock, run=runner, concurrency=1)
    await scheduler.tick()
    await until(lambda: len(runner.started) == 1)

    await queue.dismiss("acme/web", 2)
    runner.release(1)
    await scheduler.drain()

    assert runner.numbers == [1]


async def test_module_jobs_are_no_ops_once_stopped(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    module = ReviewModule()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.queue
    await module.sync_queue()
    await module.prune_workspace()
    conn = open_db(cfg.data_dir, REVIEW_DB)
    SqliteReviewStore(conn).start_round(PrRef("acme/web", 1), HEAD, BASE, "quick", START)
    conn.close()
    caplog.set_level("INFO")

    await module.start(AppContext(cfg, EventBus(), CoreScheduler()))
    await module.stop()

    assert "left running by a stop" in caplog.text


async def test_only_loaded_prs_of_a_suggested_repo_are_pre_reviewed(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1), pull(2)]
    await queue.sync()
    store.upsert_repo(RepoConfig("acme/web", suggested=True))  # demoted after rows were cached
    await queue.load("acme/web#2")  # the author's own action
    clock.advance(11)
    runner = Runner(block=False)
    scheduler = Scheduler(store, clock, run=runner)

    await scheduler.tick()
    await scheduler.drain()

    assert runner.numbers == [2]
    (row,) = [r for r in (await queue.list(QueueFilter())).rows if r.number == 1]
    assert row.prereview_state == "not_started" and "suggested" in row.prereview_reason


async def test_opens_share_the_slots_and_cancelled_waiters_give_them_back(
    store: SqliteReviewStore, clock: FakeClock
) -> None:
    """R52: ``run_open`` holds one of the scheduler's slots; a waiter cancelled before or
    just after it was granted a slot never keeps one."""
    scheduler = Scheduler(store, clock, concurrency=1)
    gate = asyncio.Event()
    holder = asyncio.create_task(scheduler.run_open(gate.wait))
    await asyncio.sleep(0)

    async def done() -> str:
        return "ran"

    # Cancelled while waiting: it leaves the line.
    left = asyncio.create_task(scheduler.run_open(done))
    await asyncio.sleep(0)
    left.cancel()
    with pytest.raises(asyncio.CancelledError):
        await left

    # Cancelled while waiting, and the slot freed before its cancellation is handled.
    skipped = asyncio.create_task(scheduler.run_open(done))
    await asyncio.sleep(0)
    gate.set()
    skipped.cancel()
    await holder
    with pytest.raises(asyncio.CancelledError):
        await skipped

    # Granted the slot, then cancelled before it could use it.
    gate.clear()
    holder = asyncio.create_task(scheduler.run_open(gate.wait))
    await asyncio.sleep(0)
    granted = asyncio.create_task(scheduler.run_open(done))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.sleep(0)  # the holder finishes and hands its slot over
    granted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await granted
    await holder

    assert await asyncio.wait_for(scheduler.run_open(done), timeout=1) == "ran"
