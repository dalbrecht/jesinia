# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue: synced from GitHub into review.sqlite, always served from it."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from jesinia.apps.review.application.queue import SYNC_INTERVAL, QueueService
from jesinia.apps.review.application.scheduler import PreReviewRequest, Scheduler
from jesinia.apps.review.domain.queue import (
    EvidenceSummary,
    InvalidPrRefError,
    InvalidSettingError,
    PreReviewMode,
    PrHeadMovedError,
    PrNotInQueueError,
    PrNotOpenError,
    PrRef,
    QueueFilter,
    RepoConfig,
    coverage_from_json,
    evidence_from_json,
    parse_pr_ref,
)
from jesinia.apps.review.domain.workspace import InvalidShaError
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.module import REVIEW_DB
from jesinia.core.db import open_db
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import (
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubUnavailable,
)
from tests.apps.review.fakes import BASE, HEAD, START, FakeClock, FakePullRequests, pull, review

OTHER_HEAD = "c" * 40


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


async def test_sync_lists_every_inbox_field(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    store.upsert_repo(RepoConfig("acme/api", watched=True, automated_reviewer="Codex[bot]"))
    github.inbox = [
        pull(1, author="me", additions=150, deletions=60, files=3),
        pull(2, "acme/api", head=OTHER_HEAD, author="renovate[bot]", bot=True, requested=True),
        pull(3, author="bob", files=12),
    ]
    github.ci = {OTHER_HEAD: "failing"}
    github.reviews_of = {
        ("acme/api", 2): [review("codex[bot]", bot=True, sha=OTHER_HEAD, comments=3)],
        ("acme/web", 3): [review("carol", comments=2), review("me", comments=1)],
    }

    await queue.sync()
    view = await queue.list(QueueFilter())

    assert view.synced_at == START and not view.syncing and view.sync_error is None
    rows = {row.number: row for row in view.rows}
    assert [row.number for row in view.rows] == [2, 1, 3]  # requested first, then oldest
    mine, agents, theirs = rows[1], rows[2], rows[3]
    kinds = (mine.author_kind, agents.author_kind, theirs.author_kind)
    assert kinds == ("you", "agent", "developer")
    assert (mine.size_lines, mine.files, mine.depth) == (210, 3, "deep")
    assert (theirs.depth, agents.depth) == ("deep", "quick")
    assert agents.ci_state == "failing" and mine.ci_state == "passing"
    assert agents.auto_review_state == "landed" and mine.auto_review_state == "not_configured"
    assert (agents.review_comment_count, agents.bot_review_count) == (3, 1)
    assert (theirs.review_comment_count, theirs.human_review_count, theirs.bot_review_count) == (
        3,
        1,
        0,
    )
    assert agents.review_requested and not mine.review_requested
    assert mine.kind == "new" and mine.created_at == START - timedelta(days=1)
    assert mine.url == "https://github.com/acme/web/pull/1" and mine.head_sha == HEAD
    assert mine.recommendation is None and mine.evidence is None and mine.coverage_pct is None
    assert mine.prereview_state == "debouncing" and mine.fetch_error is None
    assert sorted(github.inbox_calls[0]) == ["acme/api", "acme/web"]


async def test_auto_review_on_an_older_head_is_still_pending(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    store.upsert_repo(RepoConfig("acme/web", watched=True, automated_reviewer="codex"))
    github.inbox = [pull(1)]
    github.reviews_of = {("acme/web", 1): [review("codex[bot]", bot=True, sha=OTHER_HEAD)]}
    await queue.sync()
    (row,) = (await queue.list(QueueFilter())).rows
    assert row.auto_review_state == "pending"


async def test_list_filters_by_repo_in_any_case(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    store.upsert_repo(RepoConfig("acme/api", watched=True))
    github.inbox = [pull(1), pull(2, "acme/api")]
    await queue.sync()
    rows = (await queue.list(QueueFilter(repo="ACME/Api"))).rows
    assert [(row.repo, row.number) for row in rows] == [("acme/api", 2)]


async def test_load_accepts_url_shorthand_and_route(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(7), pull(10, requested=True)]
    github.others = {
        ("acme/api", 8): pull(8, "acme/api"),
        ("acme/tools", 9): pull(9, "acme/tools"),
    }
    await queue.sync()

    by_url = await queue.load("https://github.com/acme/web/pull/7/files?w=1#diff")
    by_shorthand = await queue.load("acme/api#8")
    by_route = await queue.load("http://127.0.0.1:5374/review/acme/tools/9")

    assert [(r.repo, r.number, r.loaded) for r in (by_url, by_shorthand, by_route)] == [
        ("acme/web", 7, True),
        ("acme/api", 8, True),
        ("acme/tools", 9, True),
    ]
    rows = (await queue.list(QueueFilter())).rows
    assert [row.number for row in rows][-1] == 10  # loaded rows lead, even a requested one
    assert {row.number for row in rows if row.loaded} == {7, 8, 9}


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("https://github.com/Acme/Web/pull/12", PrRef("acme/web", 12)),
        ("github.com/acme/web/pull/12/", PrRef("acme/web", 12)),
        ("https://www.github.com/acme/web/pull/12/commits", PrRef("acme/web", 12)),
        ("  acme/web#12 ", PrRef("acme/web", 12)),
        ("/review/acme/web/12", PrRef("acme/web", 12)),
        ("review/acme/web.js/12", PrRef("acme/web.js", 12)),
        ("http://127.0.0.1:5374/review/acme/web/12?tab=files", PrRef("acme/web", 12)),
    ],
)
def test_parse_pr_ref_forms(ref: str, expected: PrRef) -> None:
    assert parse_pr_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "acme/web",
        "acme/web#0",
        "acme/web#x",
        "https://github.com/acme/web/issues/12",
        "https://gitlab.com/acme/web/pull/12",
        "-acme/web#1",
        "acme/..#1",
        "/review/acme/web",
        "acme/web#1" + "1" * 30,
    ],
)
def test_parse_pr_ref_refuses_anything_else(ref: str) -> None:
    with pytest.raises(InvalidPrRefError):
        parse_pr_ref(ref)


async def test_load_refuses_closed_prs_and_surfaces_github_failures(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.others = {("acme/web", 5): pull(5, state="merged")}
    with pytest.raises(PrNotOpenError, match="merged"):
        await queue.load("acme/web#5")
    with pytest.raises(GitHubRequestError, match="Not Found"):
        await queue.load("acme/web#6")
    github.error = GitHubUnavailable("no token")
    with pytest.raises(GitHubUnavailable):
        await queue.load("acme/web#6")
    assert (await queue.list(QueueFilter())).rows == []


async def test_loading_a_cached_pr_needs_no_github_and_undismisses_it(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    await queue.dismiss("acme/web", 1)
    github.error = GitHubRequestError(None, "network is down")

    row = await queue.load("acme/web#1")

    assert row.loaded and not row.dismissed


async def test_dismiss_hides_the_row_until_it_is_loaded_again(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1), pull(2)]
    await queue.sync()
    await queue.load("acme/web#2")

    await queue.dismiss("ACME/web", 2)
    await queue.sync()

    assert [row.number for row in (await queue.list(QueueFilter())).rows] == [1]
    hidden = (await queue.list(QueueFilter(include_dismissed=True))).rows
    assert [(row.number, row.dismissed, row.loaded) for row in hidden] == [
        (1, False, False),
        (2, True, False),
    ]
    with pytest.raises(PrNotInQueueError):
        await queue.dismiss("acme/web", 99)


async def test_list_serves_cache_while_github_is_unreachable(
    queue: QueueService, github: FakePullRequests, clock: FakeClock
) -> None:
    github.inbox = [pull(1), pull(2)]
    await queue.sync()
    before = await queue.list(QueueFilter())
    clock.advance(2)
    github.error = GitHubRequestError(None, "ConnectError: network is unreachable")

    await queue.sync()  # never raises: the failure is reported in the view
    view = await queue.list(QueueFilter())

    assert view.rows == before.rows
    assert view.synced_at == START
    assert view.sync_error is not None and "network is unreachable" in view.sync_error
    assert not view.syncing

    github.error = None
    clock.advance(2)
    await queue.sync()
    recovered = await queue.list(QueueFilter())
    assert recovered.sync_error is None and recovered.synced_at == clock.now


async def test_sync_state_survives_a_restart(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    github.error = GitHubUnavailable("no GitHub token: run `gh auth login`")
    await queue.sync()

    restarted = await QueueService(github, store, clock).list(QueueFilter())

    assert restarted.synced_at == START and [r.number for r in restarted.rows] == [1]
    assert restarted.sync_error is not None and "gh auth login" in restarted.sync_error


async def test_list_never_awaits_sync(queue: QueueService, github: FakePullRequests) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    github.inbox = [pull(1), pull(2)]
    github.hold = asyncio.Event()

    syncing = asyncio.create_task(queue.sync())
    while not github.inbox_calls[1:]:
        await asyncio.sleep(0)
    view = await asyncio.wait_for(queue.list(QueueFilter()), timeout=1)

    assert view.syncing and [row.number for row in view.rows] == [1]
    await queue.sync()  # a second sync while one runs returns at once
    github.hold.set()
    await syncing
    after = await queue.list(QueueFilter())
    assert not after.syncing and [row.number for row in after.rows] == [1, 2]


async def test_rate_limited_sync_keeps_the_cache_and_backs_off(
    queue: QueueService, github: FakePullRequests, clock: FakeClock
) -> None:
    github.inbox = [pull(1, title="Cached title")]
    await queue.sync()
    reset = clock.now + timedelta(minutes=30)
    github.inbox = [pull(1, title="Fresh title", head=OTHER_HEAD)]
    github.error = GitHubRateLimitedError(403, "API rate limit exceeded", reset)
    clock.advance(2)

    await queue.sync()

    view = await queue.list(QueueFilter())
    (row,) = view.rows
    assert (row.title, row.head_sha) == ("Cached title", HEAD)
    assert view.sync_error is not None and "rate limit" in view.sync_error
    assert reset.isoformat() in view.sync_error
    calls = len(github.inbox_calls)

    github.error = None
    clock.advance(2)
    await queue.sync()  # still before the reset: GitHub isn't asked again
    assert len(github.inbox_calls) == calls

    clock.now = reset
    await queue.sync()
    assert (await queue.list(QueueFilter())).rows[0].title == "Fresh title"


async def test_a_rate_limit_in_one_prs_details_aborts_the_whole_sync(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1), pull(2, title="Before")]
    await queue.sync()
    github.inbox = [pull(1, title="After"), pull(2, title="After")]
    github.detail_errors = {("acme/web", 2): GitHubRateLimitedError(429, "slow down", None)}

    await queue.sync()

    view = await queue.list(QueueFilter())
    assert {row.title for row in view.rows} == {"PR 1", "Before"}
    assert view.sync_error is not None and "slow down" in view.sync_error


async def test_fetch_error_rows_are_flagged_and_never_overwrite_cached_fields(
    queue: QueueService, github: FakePullRequests, clock: FakeClock
) -> None:
    github.inbox = [pull(1, title="Good", additions=40, files=4)]
    await queue.sync()
    clock.advance(2)
    github.inbox = [pull(1, title="From the search hit", fetch_error="GitHub answered 502: oops")]

    await queue.sync()

    (row,) = (await queue.list(QueueFilter())).rows
    assert (row.title, row.head_sha, row.size_lines, row.files) == ("Good", HEAD, 45, 4)
    assert row.fetch_error == "GitHub answered 502: oops" and row.fetch_error_at == clock.now
    assert row.prereview_state == "fetch_failed" and "oops" in row.prereview_reason

    clock.advance(2)
    github.inbox = [pull(1, title="Good again")]
    await queue.sync()
    (row,) = (await queue.list(QueueFilter())).rows
    assert row.title == "Good again" and row.fetch_error is None and row.fetch_error_at is None


async def test_a_pr_whose_details_fail_is_flagged_without_changing_it(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1, title="Cached")]
    await queue.sync()
    github.inbox = [pull(1, title="New", head=OTHER_HEAD)]
    github.detail_errors = {("acme/web", 1): GitHubPayloadError("expected a list")}

    await queue.sync()

    (row,) = (await queue.list(QueueFilter())).rows
    assert (row.title, row.head_sha) == ("Cached", HEAD)
    assert row.fetch_error is not None and "expected a list" in row.fetch_error


async def test_new_fetch_error_rows_are_listed_flagged(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    github.inbox = [pull(4, title="Can't open", fetch_error="GitHub answered 403: no")]

    await queue.sync()

    (row,) = (await queue.list(QueueFilter())).rows
    assert (row.number, row.title, row.head_sha) == (4, "Can't open", "")
    assert row.prereview_state == "fetch_failed"
    assert store.keep_shas() == set()  # nothing on disk to keep for it


async def test_mixed_case_repo_is_one_row_end_to_end(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    store.upsert_repo(RepoConfig("Acme/Web", watched=True, automated_reviewer="codex"))
    github.inbox = [pull(7, "Acme/Web")]
    await queue.sync()

    loaded = await queue.load("ACME/WEB#7")
    await queue.sync()

    rows = (await queue.list(QueueFilter(repo="acme/WEB"))).rows
    assert [(row.repo, row.display_repo, row.number) for row in rows] == [
        ("acme/web", "Acme/Web", 7)
    ]
    assert loaded.repo == "acme/web" and rows[0].loaded
    assert rows[0].auto_review_state == "pending"  # the repo's config applied across cases
    assert store.watched_repos() == ["acme/web"]
    assert github.inbox_calls[-1] == ["acme/web"]
    assert store.keep_shas() == {("acme/web", HEAD), ("acme/web", BASE)}
    await queue.dismiss("Acme/WEB", 7)
    assert (await queue.list(QueueFilter())).rows == []


async def test_closed_prs_leave_the_queue_and_loaded_ones_stay_while_open(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1), pull(2)]
    github.others = {("acme/other", 3): pull(3, "acme/other")}
    await queue.sync()
    await queue.load("acme/other#3")

    github.inbox = [pull(2)]
    await queue.sync()
    assert [row.number for row in (await queue.list(QueueFilter())).rows] == [3, 2]

    github.others = {("acme/other", 3): pull(3, "acme/other", state="closed")}
    await queue.sync()
    assert [row.number for row in (await queue.list(QueueFilter())).rows] == [2]


async def test_a_loaded_pr_that_fails_to_refresh_is_flagged(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.others = {("acme/other", 3): pull(3, "acme/other")}
    await queue.load("acme/other#3")
    github.others = {}

    await queue.sync()

    (row,) = (await queue.list(QueueFilter())).rows
    assert row.fetch_error is not None and "Not Found" in row.fetch_error
    assert row.head_sha == HEAD


async def test_a_head_change_restarts_the_debounce(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    first = store.pr(PrRef("acme/web", 1))
    clock.advance(5)
    await queue.sync()
    same = store.pr(PrRef("acme/web", 1))
    clock.advance(5)
    github.inbox = [pull(1, head=OTHER_HEAD)]
    await queue.sync()
    moved = store.pr(PrRef("acme/web", 1))

    assert first is not None and same is not None and moved is not None
    assert first.last_head_change_at == same.last_head_change_at == START
    assert moved.last_head_change_at == clock.now and moved.state == "debouncing"


async def test_rows_carry_the_latest_round_recommendation_evidence_and_coverage(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    github.inbox = [pull(1), pull(2)]
    await queue.sync()
    old = store.start_round(PrRef("acme/web", 1), OTHER_HEAD, BASE, "quick", clock.now)
    store.finish_round(old, clock.now, None, verdict="request_changes")
    current = store.start_round(PrRef("acme/web", 1), HEAD, BASE, "deep", clock.now)
    store.put_artifact(
        current, "evidence", {"ci": "passing", "tests": "42 passed", "findings": {"style": 2}}
    )
    store.put_artifact(current, "coverage", {"pct": 87.5})
    store.finish_round(current, clock.now, None, verdict="approve")
    broken = store.start_round(PrRef("acme/web", 2), OTHER_HEAD, BASE, "quick", clock.now)
    store.put_artifact(broken, "evidence", {"ci": 3})
    store.put_artifact(broken, "coverage", ["not", "a", "mapping"])
    store.finish_round(broken, clock.now, None, verdict="approve")

    rows = {row.number: row for row in (await queue.list(QueueFilter())).rows}

    assert rows[1].recommendation == "approve" and not rows[1].recommendation_stale
    assert rows[1].evidence == EvidenceSummary("passing", "42 passed", {"style": 2})
    assert rows[1].coverage_pct == 87.5
    assert rows[2].recommendation == "approve" and rows[2].recommendation_stale
    assert rows[2].evidence is None and rows[2].coverage_pct is None


async def test_files_are_served_from_the_cache_after_the_first_fetch(
    queue: QueueService, github: FakePullRequests, conn: sqlite3.Connection
) -> None:
    github.inbox = [pull(1)]
    first = await queue.files("Acme/Web", 1, HEAD)
    github.error = GitHubRequestError(None, "offline")
    second = await queue.files("acme/web", 1, HEAD)

    assert first == second and github.file_calls == 1
    assert first[0].filename == "src/app.py"
    (stored,) = conn.execute("SELECT repo, json FROM pr_files").fetchall()
    assert stored[0] == "acme/web" and json.loads(stored[1])[0]["filename"] == "src/app.py"


async def test_sync_with_nothing_watched_asks_github_for_no_inbox(
    conn: sqlite3.Connection, github: FakePullRequests, clock: FakeClock
) -> None:
    queue = QueueService(github, SqliteReviewStore(conn), clock)
    await queue.sync()
    assert github.inbox_calls == []
    view = await queue.list(QueueFilter())
    assert view.rows == [] and view.synced_at == START


async def test_sync_and_load_notify_the_scheduler(
    github: FakePullRequests, store: SqliteReviewStore, clock: FakeClock
) -> None:
    calls: list[str] = []

    async def changed() -> None:
        calls.append("tick")

    queue = QueueService(github, store, clock, on_change=changed)
    github.inbox = [pull(1)]
    await queue.sync()
    await queue.load("acme/web#1")
    github.error = GitHubRequestError(None, "offline")
    await queue.sync()  # a failed sync still ticks: debounces advance on cached data
    assert calls == ["tick", "tick", "tick"]


def test_repo_config_validates_its_values() -> None:
    assert RepoConfig("Acme/Web").repo == "acme/web"
    with pytest.raises(InvalidPrRefError):
        RepoConfig("not a repo")
    with pytest.raises(InvalidSettingError, match="pre-review"):
        RepoConfig("acme/web", prereview=cast(PreReviewMode, "sometimes"))


async def test_a_rate_limit_refreshing_a_loaded_pr_aborts_the_sync(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.others = {("acme/other", 3): pull(3, "acme/other", title="Cached")}
    await queue.load("acme/other#3")
    github.others = {}
    github.get_error = GitHubRateLimitedError(429, "slow down", None)

    await queue.sync()

    view = await queue.list(QueueFilter())
    assert [row.title for row in view.rows] == ["Cached"] and view.rows[0].fetch_error is None
    assert view.sync_error is not None and "slow down" in view.sync_error


def test_pr_ref_needs_a_positive_number() -> None:
    assert str(PrRef("Acme/Web", 3)) == "acme/web#3"
    with pytest.raises(InvalidPrRefError, match="start at 1"):
        PrRef("acme/web", 0)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"ci": "passing"}, EvidenceSummary("passing", None, {})),
        ({"ci": "passing", "tests": 3}, None),
        ({"ci": "passing", "findings": ["style"]}, None),
        ({"ci": "passing", "findings": {"style": "two"}}, None),
        ({"ci": "passing", "findings": {"style": True}}, None),
        ("passing", None),
    ],
)
def test_evidence_artifacts_of_another_shape_are_ignored(
    data: object, expected: EvidenceSummary | None
) -> None:
    assert evidence_from_json(data) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [({"pct": 100}, 100.0), ({"pct": 101}, None), ({"pct": True}, None), ({"pct": "9"}, None)],
)
def test_coverage_artifacts_of_another_shape_are_ignored(
    data: object, expected: float | None
) -> None:
    assert coverage_from_json(data) == expected


async def test_a_pr_loaded_during_a_sync_survives_leaving_the_inbox(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1), pull(2)]
    await queue.sync()
    github.inbox = [pull(1)]  # PR 2 drops out of the inbox (review no longer requested)
    github.hold = asyncio.Event()
    syncing = asyncio.create_task(queue.sync())
    while not github.inbox_calls[1:]:
        await asyncio.sleep(0)

    await queue.load("acme/web#2")
    github.hold.set()
    await syncing

    rows = (await queue.list(QueueFilter())).rows
    assert [(row.number, row.loaded) for row in rows] == [(2, True), (1, False)]


async def test_first_sync_watches_repos_with_review_requests(
    conn: sqlite3.Connection, github: FakePullRequests, clock: FakeClock
) -> None:
    store = SqliteReviewStore(conn)  # a fresh install: no repo is watched yet
    queue = QueueService(github, store, clock)
    github.orgs = ["acme"]  # the viewer belongs to acme, so its repos are the viewer's own
    github.inbox = [
        pull(1, "acme/api", requested=True),
        pull(2, "Acme/Web", author="me"),
        pull(3, "acme/old"),  # only reviewed before: not a reason to watch the repo
    ]

    await queue.sync()

    assert store.watched_repos() == ["acme/api", "acme/web"]
    rows = (await queue.list(QueueFilter())).rows
    assert sorted((row.repo, row.number) for row in rows) == [("acme/api", 1), ("acme/web", 2)]


async def test_an_unwatched_repo_is_never_reseeded(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    store.upsert_repo(RepoConfig("acme/web", watched=False))
    github.inbox = [pull(1, requested=True)]

    await queue.sync()
    await queue.sync()

    assert store.watched_repos() == []
    assert store.repo_configs()["acme/web"].watched is False
    assert (await queue.list(QueueFilter())).rows == []


async def test_a_stale_head_never_caches_the_current_heads_files(
    queue: QueueService, github: FakePullRequests, conn: sqlite3.Connection
) -> None:
    github.inbox = [pull(1, head=OTHER_HEAD)]  # the PR has moved on from HEAD

    with pytest.raises(PrHeadMovedError, match=OTHER_HEAD):
        await queue.files("acme/web", 1, HEAD)

    cached = conn.execute("SELECT head_sha FROM pr_files").fetchall()
    assert cached == [(OTHER_HEAD,)]
    assert await queue.files("acme/web", 1, OTHER_HEAD) and github.file_calls == 1
    with pytest.raises(InvalidShaError):
        await queue.files("acme/web", 1, "HEAD")


async def test_files_fetched_while_the_head_moves_are_not_cached(
    queue: QueueService, github: FakePullRequests, conn: sqlite3.Connection
) -> None:
    github.inbox = [pull(1)]
    github.heads_seen_by_get = [HEAD, OTHER_HEAD]  # a push lands between the two reads

    with pytest.raises(PrHeadMovedError):
        await queue.files("acme/web", 1, HEAD)

    assert conn.execute("SELECT COUNT(*) FROM pr_files").fetchone() == (0,)


async def test_files_leave_the_cache_with_their_pr(
    queue: QueueService, github: FakePullRequests, conn: sqlite3.Connection
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    await queue.files("acme/web", 1, HEAD)
    github.inbox = []

    await queue.sync()

    assert conn.execute("SELECT COUNT(*) FROM pr_files").fetchone() == (0,)


async def test_a_rate_limit_without_a_reset_time_still_backs_off(
    queue: QueueService, github: FakePullRequests, clock: FakeClock
) -> None:
    github.inbox = [pull(1)]
    github.error = GitHubRateLimitedError(403, "secondary rate limit", None)
    await queue.sync()
    calls = len(github.inbox_calls)
    github.error = None

    clock.advance(1)
    await queue.sync()
    assert len(github.inbox_calls) == calls
    clock.advance(SYNC_INTERVAL.total_seconds() / 60)
    await queue.sync()
    assert len(github.inbox_calls) == calls + 1


async def test_a_domain_error_during_sync_is_reported_not_raised(
    queue: QueueService, github: FakePullRequests
) -> None:
    github.inbox = [pull(1)]
    await queue.sync()
    github.inbox = [pull(1, additions=-50)]  # GitHub can't send this, but nothing may crash

    await queue.sync()

    view = await queue.list(QueueFilter())
    assert [row.number for row in view.rows] == [1]
    assert view.sync_error is not None and "negative" in view.sync_error


async def test_a_slow_sync_never_overwrites_a_newer_load(
    queue: QueueService, github: FakePullRequests, store: SqliteReviewStore
) -> None:
    github.inbox = [pull(2, head=HEAD)]
    github.detail_holds = {HEAD: asyncio.Event()}
    syncing = asyncio.create_task(queue.sync())
    while not github.inbox_calls:
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    github.inbox = [pull(2, head=OTHER_HEAD)]  # pushed: the load reads the new head
    loaded = await queue.load("acme/web#2")
    github.detail_holds[HEAD].set()
    await syncing

    stored = store.pr(PrRef("acme/web", 2))
    assert loaded.head_sha == OTHER_HEAD
    assert stored is not None and stored.pr.head_sha == OTHER_HEAD and stored.loaded


@pytest.fixture
def fresh_queue(
    conn: sqlite3.Connection, github: FakePullRequests, clock: FakeClock
) -> tuple[QueueService, SqliteReviewStore]:
    store = SqliteReviewStore(conn)
    return QueueService(github, store, clock), store


async def test_the_viewers_own_repos_are_auto_watched(
    fresh_queue: tuple[QueueService, SqliteReviewStore], github: FakePullRequests
) -> None:
    queue, store = fresh_queue
    github.inbox = [pull(1, "Me/Tool", requested=True)]

    await queue.sync()

    assert store.watched_repos() == ["me/tool"]
    view = await queue.list(QueueFilter())
    assert [row.repo for row in view.rows] == ["me/tool"] and view.suggested_repos == []


async def test_repos_of_the_viewers_orgs_are_auto_watched(
    fresh_queue: tuple[QueueService, SqliteReviewStore], github: FakePullRequests
) -> None:
    queue, store = fresh_queue
    github.orgs = ["acme"]
    github.inbox = [pull(2, "Acme/Web", requested=True)]

    await queue.sync()

    assert store.watched_repos() == ["acme/web"]


async def test_a_strangers_repo_is_only_suggested_and_never_scheduled(
    fresh_queue: tuple[QueueService, SqliteReviewStore],
    github: FakePullRequests,
    clock: FakeClock,
) -> None:
    queue, store = fresh_queue
    github.inbox = [pull(3, "evil/bait", requested=True)]

    await queue.sync()
    clock.advance(60)
    await queue.sync()
    started: list[PreReviewRequest] = []

    async def run(request: PreReviewRequest) -> None:
        started.append(request)

    scheduler = Scheduler(store, clock, run=run)
    await scheduler.tick()
    await scheduler.drain()

    view = await queue.list(QueueFilter())
    assert store.watched_repos() == [] and view.suggested_repos == ["evil/bait"]
    assert view.rows == [] and started == [] and store.keep_shas() == set()
    assert all("evil" not in repo for call in github.inbox_calls for repo in call)


async def test_confirming_a_suggested_repo_watches_it(
    fresh_queue: tuple[QueueService, SqliteReviewStore], github: FakePullRequests
) -> None:
    queue, store = fresh_queue
    github.inbox = [pull(3, "evil/bait", requested=True)]
    await queue.sync()

    await queue.confirm_repo("Evil/Bait")
    await queue.sync()

    view = await queue.list(QueueFilter())
    assert store.watched_repos() == ["evil/bait"] and view.suggested_repos == []
    assert [(row.repo, row.suggested) for row in view.rows] == [("evil/bait", False)]
    store.upsert_repo(RepoConfig("evil/bait", watched=False))  # the author unwatches it again
    await queue.sync()
    assert store.watched_repos() == [] and (await queue.list(QueueFilter())).suggested_repos == []


async def test_loading_a_strangers_pr_works_without_watching_the_repo(
    fresh_queue: tuple[QueueService, SqliteReviewStore], github: FakePullRequests
) -> None:
    queue, store = fresh_queue
    github.inbox = [pull(3, "evil/bait", requested=True)]
    github.others = {("evil/bait", 4): pull(4, "evil/bait")}
    await queue.sync()

    row = await queue.load("evil/bait#4")
    await queue.sync()

    assert row.loaded and row.suggested
    assert store.watched_repos() == []
    assert [r.number for r in (await queue.list(QueueFilter())).rows] == [4]


async def test_the_viewers_orgs_are_fetched_at_most_hourly(
    queue: QueueService, github: FakePullRequests, clock: FakeClock
) -> None:
    await queue.sync()
    clock.advance(30)
    await queue.sync()
    assert github.org_calls == 1
    clock.advance(31)
    await queue.sync()
    assert github.org_calls == 2
