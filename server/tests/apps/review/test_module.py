# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review module skeleton: owns review.sqlite (history) and the repo workspace."""

from __future__ import annotations

import logging
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

    # A stack job a stop cut short is marked interrupted at start (R73).
    from jesinia.apps.review.infrastructure.store import SqliteReviewStore

    review_db = open_db(cfg.data_dir, REVIEW_DB)
    review_store = SqliteReviewStore(review_db)
    stack_id = review_store.start_stack_review("acme/web", "1@a,2@b", datetime.now(UTC))
    review_store.put_stack_artifact(stack_id, "jobs", {"overview": {"state": "running"}})
    review_db.close()

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

    # A stack job a stop cut short is marked interrupted at start (R73).
    from jesinia.apps.review.infrastructure.store import SqliteReviewStore

    review_db = open_db(cfg.data_dir, REVIEW_DB)
    review_store = SqliteReviewStore(review_db)
    stack_id = review_store.start_stack_review("acme/web", "1@a,2@b", datetime.now(UTC))
    review_store.put_stack_artifact(stack_id, "jobs", {"overview": {"state": "running"}})
    review_db.close()

    conn = open_db(tmp_path / "agents", SESSIONS_DB)
    agents = SessionService(SqliteSessionStore(conn), {}, tmp_path / "s", "http://x/mcp", dict)
    ctx = AppContext(cfg, EventBus(), Scheduler())
    ctx.services["agents"] = agents
    ctx.services["github"] = GitHubService(no_token, NoCache())
    await module.start(ctx)
    assert isinstance(module.pipeline, Pipeline)
    assert isinstance(module.posting, PostingService)
    # M5: a post hands its round on without waiting for the keep.
    assert module.posting._on_posted == module.keep_in_background
    assert isinstance(module.stacks, StackReviews)
    swept = module.stacks._store.stack_jobs()[stack_id]
    assert isinstance(swept, dict) and swept["overview"]["state"] == "interrupted"
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


async def test_the_knowledge_model_starts_with_the_module(cfg: Config) -> None:
    module = ReviewModule()
    with pytest.raises(HTTPException) as caught:
        _ = module.knowledge
    assert caught.value.status_code == 503
    await module.ingest_knowledge()  # before start: nothing to do

    scheduler = Scheduler()
    await module.start(AppContext(cfg, EventBus(), scheduler))
    await module.ingest_knowledge()  # no KB, no GitHub, no mirrors: nothing read, no error
    assert module.knowledge.view().total == 0
    await module.stop()
    with pytest.raises(HTTPException):
        _ = module.knowledge


async def test_the_emails_key_is_made_once_per_install_and_kept_private(cfg: Config) -> None:
    """M3: the knowledge model's emails digest is keyed by a per-install secret file."""
    import stat

    module = ReviewModule()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    await module.stop()
    key = cfg.data_dir / "review" / "knowledge.key"
    first = key.read_bytes()
    assert len(first) == 32 and stat.S_IMODE(key.stat().st_mode) == 0o600
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    await module.stop()
    assert key.read_bytes() == first


@pytest.mark.parametrize("content", [b"", b"short", b"x" * 33])
async def test_a_corrupt_emails_key_is_made_again_and_said(
    cfg: Config, caplog: pytest.LogCaptureFixture, content: bytes
) -> None:
    """M3: a key of the wrong length is replaced (a full history re-read follows); the log
    says why, without key material."""
    key = cfg.data_dir / "review" / "knowledge.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(content)
    module = ReviewModule()
    with caplog.at_level(logging.WARNING):
        await module.start(AppContext(cfg, EventBus(), Scheduler()))
    await module.stop()
    made = key.read_bytes()
    assert len(made) == 32 and made != content
    assert "knowledge.key" in caplog.text and "re-read" in caplog.text
    assert made.hex() not in caplog.text and repr(made) not in caplog.text


async def test_a_missing_emails_key_is_made_and_said(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    module = ReviewModule()
    with caplog.at_level(logging.INFO):
        await module.start(AppContext(cfg, EventBus(), Scheduler()))
    await module.stop()
    assert "made a new knowledge.key" in caplog.text


async def test_the_emails_key_is_written_atomically(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: written to a private temp file, synced, then renamed into place: a crash never
    leaves a short key behind."""
    import os

    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace(src: str, dst: str) -> None:
        calls.append("replace")
        assert oct(os.stat(src).st_mode & 0o777) == oct(0o600) and os.path.getsize(src) == 32
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    module = ReviewModule()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    await module.stop()
    assert calls == ["fsync", "replace"]
    assert not [p for p in (cfg.data_dir / "review").iterdir() if p.name.startswith(".knowledge")]


async def test_an_unreadable_emails_key_never_stops_the_app(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """M3: a key that can't be read or written (a folder in its place) is logged and an
    in-memory key used; the review app still starts."""
    (cfg.data_dir / "review" / "knowledge.key").mkdir(parents=True)
    module = ReviewModule()
    with caplog.at_level(logging.WARNING):
        await module.start(AppContext(cfg, EventBus(), Scheduler()))
    assert module.health().ok and module.knowledge.view().total == 0
    assert "knowledge.key" in caplog.text
    await module.stop()
