# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Review app module: owns ``review.sqlite``, the queue and its pre-review scheduler,
the repo workspace, the repo tools and the agent jobs. Starts last (after ``agents`` and
``github``)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.review.api.tools import build_tools
from jesinia.apps.review.application.jobs import JobRunner
from jesinia.apps.review.application.pipeline import Pipeline
from jesinia.apps.review.application.queue import SYNC_INTERVAL, QueueService
from jesinia.apps.review.application.repo_tools import RepoTools
from jesinia.apps.review.application.scheduler import (
    PRUNE_INTERVAL,
    PreReviewRequest,
    Scheduler,
    WorkspacePruner,
)
from jesinia.apps.review.application.sessions import ReviewSessionRegistry
from jesinia.apps.review.infrastructure.standards_loader import StandardsLoader
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.infrastructure.workspace import GitWorkspace, github_remote_url
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.github.application.prs import PrClient
from jesinia.services.github.application.service import GitHubService

log = logging.getLogger(__name__)

# History: rounds, drafts, posted reviews and the knowledge model can't be rebuilt from
# GitHub, and `jesinia backup` copies only history databases.
REVIEW_DB = ModuleDb("review", DbClass.HISTORY, files("jesinia.apps.review.migrations"))
SYNC_JOB = "review.sync"
PRUNE_JOB = "review.prune"


def _not_started() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "review module not started")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ReviewModule:
    name = "review"

    def __init__(self) -> None:
        self._workspace: GitWorkspace | None = None
        self._conn: sqlite3.Connection | None = None
        self._repo_tools: RepoTools | None = None
        self._queue: QueueService | None = None
        self._scheduler: Scheduler | None = None
        self._pruner: WorkspacePruner | None = None
        self._jobs: JobRunner | None = None
        self._pipeline: Pipeline | None = None
        # Review jobs bind their agent sessions here; the repo tools read through it.
        self.sessions = ReviewSessionRegistry()

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            raise _not_started()
        return self._workspace

    @property
    def repo_tools(self) -> RepoTools:
        if self._repo_tools is None:
            raise _not_started()
        return self._repo_tools

    @property
    def queue(self) -> QueueService:
        if self._queue is None:
            if self._scheduler is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "the review queue requires the github service",
                )
            raise _not_started()
        return self._queue

    @property
    def jobs(self) -> JobRunner:
        """Runs review agent jobs (needs the agents service)."""
        if self._jobs is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "review jobs require the agents service",
                )
            raise _not_started()
        return self._jobs

    @property
    def pipeline(self) -> Pipeline:
        """Pre-reviews and opening PRs (needs the agents and github services)."""
        if self._pipeline is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "reviews require the agents service and the github service",
                )
            raise _not_started()
        return self._pipeline

    async def _prereview(self, request: PreReviewRequest) -> None:
        await self.pipeline.prereview(request)

    @property
    def scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise _not_started()
        return self._scheduler

    async def sync_queue(self) -> None:
        """Refresh the queue from GitHub (the ``review.sync`` job); the scheduler ticks after."""
        if self._queue is not None:
            await self._queue.sync()

    async def prune_workspace(self) -> None:
        """Drop worktrees nothing needs (the daily ``review.prune`` job)."""
        if self._pruner is not None:
            await self._pruner.run()

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, REVIEW_DB)
        self._workspace = GitWorkspace(ctx.cfg.data_dir / "review" / "repos", github_remote_url)
        self._repo_tools = RepoTools(
            self.sessions.worktree_for,
            self._workspace,
            self.sessions.allowance,
            git_slots=self.sessions.git_slot,
            tree_key=self._workspace.tree_key,
            reads=self.sessions.reads,
        )
        self.sessions.use_tree_locks(self._workspace.tree_lock)
        store = SqliteReviewStore(self._conn)
        agents = ctx.services.get("agents")
        github = ctx.services.get("github")
        reviews = isinstance(agents, SessionService) and isinstance(github, GitHubService)
        # The review pipeline runs the pre-reviews; without agents and GitHub the gates are
        # still applied and ready PRs show as queued.
        self._scheduler = Scheduler(store, _utc_now, run=self._prereview if reviews else None)
        if interrupted := self._scheduler.recover():
            log.info("re-queued %d review rounds left running by a stop", interrupted)
        self._pruner = WorkspacePruner(store, self._workspace, self.sessions.bound_trees)
        ctx.scheduler.every(PRUNE_JOB, PRUNE_INTERVAL, self.prune_workspace, at_start=True)
        if isinstance(agents, SessionService):
            self._jobs = JobRunner(
                agents,
                self._workspace,
                StandardsLoader(self._workspace, store.standards_dir),
                registry=self.sessions,
                store=store,
            )
        if isinstance(github, GitHubService):
            prs = PrClient(github)
            self._queue = QueueService(prs, store, _utc_now, on_change=self._scheduler.tick)
            ctx.scheduler.every(SYNC_JOB, SYNC_INTERVAL, self.sync_queue, at_start=True)
            if self._jobs is not None:
                self._pipeline = Pipeline(
                    github=prs,
                    files=self._queue,
                    workspace=self._workspace,
                    jobs=self._jobs,
                    store=store,
                    bus=ctx.bus,
                    clock=_utc_now,
                )

    async def stop(self) -> None:
        if self._pipeline is not None:
            await self._pipeline.stop()
        if self._scheduler is not None:
            await self._scheduler.stop()
        self._pipeline = None
        self._scheduler = None
        self._jobs = None
        self._queue = None
        self._pruner = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._workspace = None
        self._repo_tools = None

    def health(self) -> ModuleHealth:
        ready = self._workspace is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
