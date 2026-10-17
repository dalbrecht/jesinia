# SPDX-License-Identifier: AGPL-3.0-or-later
"""The issues module skeleton: owns issues.sqlite (history) and its idempotency keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.sync import SYNC_INTERVAL, SyncService
from jesinia.apps.issues.domain.triage import IssueFilters
from jesinia.apps.issues.module import IDEMPOTENCY_PRUNE_JOB, ISSUES_DB, SYNC_JOB, IssuesModule
from jesinia.core.config import Config
from jesinia.core.db import DbClass, open_db
from jesinia.core.events import EventBus
from jesinia.core.idempotency import SqliteIdempotencyStore
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Job, Scheduler
from jesinia.registry import DATABASES, modules
from jesinia.services.github.application.service import GitHubService, GitHubUnavailable


def test_issues_db_is_history_and_registered_after_its_services() -> None:
    assert ISSUES_DB.name == "issues" and ISSUES_DB.db_class is DbClass.HISTORY
    assert ISSUES_DB in DATABASES
    names = [m.name for m in modules()]
    assert names.index("issues") > max(names.index(n) for n in ("kb", "agents", "github"))


async def test_start_opens_the_db_and_wires_idempotency(cfg: Config) -> None:
    module = IssuesModule()
    assert module.router() is not None
    assert [tool.name for tool in module.mcp_tools()] == ["issues_search", "issues_get"]
    assert not module.health().ok
    with pytest.raises(HTTPException, match="not started") as caught:
        _ = module.idempotency
    assert caught.value.status_code == 503
    await module.prune_idempotency_keys()  # nothing to prune before start

    ctx = AppContext(cfg, EventBus(), Scheduler())
    await module.start(ctx)
    assert module.health().ok
    assert (cfg.data_dir / "issues.sqlite").exists()
    assert isinstance(module.idempotency, SqliteIdempotencyStore)
    await ctx.scheduler.run_now(IDEMPOTENCY_PRUNE_JOB)
    await module.stop()
    await module.stop()
    assert not module.health().ok

    # A key a crash left pending would block its retry with 409: start clears it.
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO idempotency (key, route, created_at) VALUES ('k', 'POST /x', '2026-10-16')"
    )
    conn.commit()
    conn.close()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    assert module.idempotency.reserve("k", "POST /x", datetime.now(UTC)) is None
    await module.stop()


class _NoCache:
    def get(self, key: str) -> None:
        return None

    def put(self, key: str, etag: str, status: int, data: object) -> None:
        return None

    def prune(self, older_than: object) -> int:
        return 0


async def _no_token() -> str | None:
    return None


async def test_sync_needs_the_github_service_and_runs_as_a_job(cfg: Config) -> None:
    module = IssuesModule()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.sync
    for name in ("labels", "queries"):
        with pytest.raises(HTTPException, match="not started"):
            getattr(module, name)
    await module.sync_repos()  # nothing to sync before start

    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    with pytest.raises(HTTPException, match="require the github service") as caught:
        _ = module.sync
    assert caught.value.status_code == 503
    with pytest.raises(HTTPException, match="require the github service"):
        _ = module.labels
    assert isinstance(module.queries, IssueQueries)  # the cache reads need no GitHub
    assert module.clock().tzinfo is not None
    await module.stop()

    ctx = AppContext(cfg, EventBus(), Scheduler())
    github = GitHubService(_no_token, _NoCache())
    ctx.services["github"] = github
    await module.start(ctx)
    assert isinstance(module.sync, SyncService)
    assert isinstance(module.labels, LabelSetupService)
    # Without a token a repo can't be added (GitHub names it), ...
    with pytest.raises(GitHubUnavailable):
        await module.sync.configure("acme/web", watched=True, settings_schema=None)
    # ... and a repo added earlier fails its sync, recorded, while the cache keeps serving.
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO repos (repo, added_at) VALUES ('acme/web', '2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    await ctx.scheduler.run_now(SYNC_JOB)
    state = module.queries.view("inbox", IssueFilters())
    assert state.rows == [] and state.sync_error is not None
    await module.stop()
    await github.aclose()


class _RecordingScheduler(Scheduler):
    def __init__(self) -> None:
        super().__init__()
        self.registered: dict[str, tuple[timedelta, bool]] = {}

    def every(self, name: str, interval: timedelta, job: Job, *, at_start: bool = False) -> None:
        self.registered[name] = (interval, at_start)
        super().every(name, interval, job, at_start=at_start)


async def test_sync_job_runs_every_five_minutes_and_at_start(cfg: Config) -> None:
    assert SYNC_INTERVAL == timedelta(minutes=5)
    scheduler = _RecordingScheduler()
    ctx = AppContext(cfg, EventBus(), scheduler)
    github = GitHubService(_no_token, _NoCache())
    ctx.services["github"] = github
    module = IssuesModule()
    await module.start(ctx)
    assert scheduler.registered[SYNC_JOB] == (timedelta(minutes=5), True)
    await module.stop()
    await github.aclose()
