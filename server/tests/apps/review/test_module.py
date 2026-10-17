# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review module skeleton: owns review.sqlite (history) and the repo workspace."""

from __future__ import annotations

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
    assert module.router() is None
    assert [t.name for t in module.mcp_tools()] == ["repo_read", "repo_grep", "repo_ls", "repo_git"]
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
