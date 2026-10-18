# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review module skeleton: owns review.sqlite (history) and the repo workspace."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from jesinia.apps.review.application.jobs import JobRunner
from jesinia.apps.review.infrastructure.workspace import GitWorkspace
from jesinia.apps.review.module import REVIEW_DB, ReviewModule
from jesinia.core.config import Config
from jesinia.core.db import DbClass, open_db
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Scheduler
from jesinia.registry import DATABASES, modules
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.agents.infrastructure.store import SqliteSessionStore
from jesinia.services.agents.module import SESSIONS_DB


def test_review_db_is_history_and_registered() -> None:
    assert REVIEW_DB.name == "review" and REVIEW_DB.db_class is DbClass.HISTORY
    assert REVIEW_DB in DATABASES
    assert [m.name for m in modules()][-1] == "review"


async def test_start_opens_the_db_and_roots_the_workspace(cfg: Config) -> None:
    module = ReviewModule()
    assert module.router() is not None
    assert [t.name for t in module.mcp_tools()] == [
        "repo_read",
        "repo_grep",
        "repo_ls",
        "repo_git",
        "review_queue",
        "review_get",
        "review_findings",
    ]
    assert not module.health().ok
    with pytest.raises(HTTPException) as caught:
        _ = module.workspace
    assert caught.value.status_code == 503

    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    assert module.health().ok
    assert isinstance(module.workspace, GitWorkspace)
    assert module.workspace.root == cfg.data_dir / "review" / "repos"
    assert (cfg.data_dir / "review.sqlite").exists()

    await module.stop()
    await module.stop()
    assert not module.health().ok


def test_root_is_not_created_until_used(tmp_path: Path) -> None:
    GitWorkspace(tmp_path / "repos", str)
    assert not (tmp_path / "repos").exists()


async def test_jobs_need_the_agents_service(cfg: Config, tmp_path: Path) -> None:
    module = ReviewModule()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    with pytest.raises(HTTPException, match="agents service"):
        _ = module.jobs
    await module.stop()

    conn = open_db(tmp_path / "agents", SESSIONS_DB)
    agents = SessionService(SqliteSessionStore(conn), {}, tmp_path / "s", "http://x/mcp", dict)
    ctx = AppContext(cfg, EventBus(), Scheduler())
    ctx.services["agents"] = agents
    await module.start(ctx)
    assert isinstance(module.jobs, JobRunner)
    await module.stop()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.jobs
    conn.close()


async def test_the_pipeline_needs_agents_and_github_and_runs_the_pre_reviews(
    cfg: Config, tmp_path: Path
) -> None:
    from jesinia.apps.review.application.pipeline import Pipeline
    from jesinia.apps.review.application.posting import PostingService
    from jesinia.apps.review.application.stacks import StackReviews
    from jesinia.services.github.application.service import GitHubService

    async def no_token() -> str | None:
        return None

    class NoCache:
        def get(self, key: str) -> None:
            return None

        def put(self, key: str, etag: str, status: int, data: object) -> None:
            return None

        def prune(self, older_than: object) -> int:
            return 0

    module = ReviewModule()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.pipeline
    with pytest.raises(HTTPException, match="not started"):
        _ = module.stacks
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    with pytest.raises(HTTPException, match="agents service and the github service"):
        _ = module.pipeline
    with pytest.raises(HTTPException, match="agents service and the github service"):
        _ = module.stacks
    await module.stop()

    conn = open_db(tmp_path / "agents", SESSIONS_DB)
    agents = SessionService(SqliteSessionStore(conn), {}, tmp_path / "s", "http://x/mcp", dict)
    ctx = AppContext(cfg, EventBus(), Scheduler())
    ctx.services["agents"] = agents
    ctx.services["github"] = GitHubService(no_token, NoCache())
    await module.start(ctx)
    assert isinstance(module.pipeline, Pipeline)
    assert isinstance(module.posting, PostingService)
    assert isinstance(module.stacks, StackReviews)
    assert module.scheduler.runs_pre_reviews
    await module.stop()
    conn.close()


async def test_posting_reviews_and_keys_need_a_started_module(cfg: Config) -> None:
    from jesinia.apps.review.application.reviews import ReviewReads
    from jesinia.core.idempotency import SqliteIdempotencyStore

    module = ReviewModule()
    for name in ("posting", "reviews", "idempotency"):
        with pytest.raises(HTTPException, match="not started"):
            getattr(module, name)
    await module.prune_idempotency_keys()  # nothing to prune before start

    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    with pytest.raises(HTTPException, match="requires the github service"):
        _ = module.posting
    assert isinstance(module.reviews, ReviewReads)
    assert isinstance(module.idempotency, SqliteIdempotencyStore)
    # Without GitHub nothing syncs: the views say so from the cache.
    assert module.reviews.pr is not None
    status = module.reviews._status()
    assert status.syncing is False and status.synced_at is None
    await module.prune_idempotency_keys()
    await module.stop()

    # A key a crash left pending would block its retry with 409: start clears it.
    conn = open_db(cfg.data_dir, REVIEW_DB)
    conn.execute(
        "INSERT INTO idempotency (key, route, created_at) VALUES ('k', 'POST /x', '2026-10-16')"
    )
    conn.close()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    assert module.idempotency.reserve("k", "POST /x", datetime.now(UTC)) is None
    await module.stop()
