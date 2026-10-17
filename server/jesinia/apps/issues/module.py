# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Issues app module: owns ``issues.sqlite`` and its idempotency keys. Starts after
``kb`` (tags), ``agents`` and ``github``, which triage, suggestions and writes use.

With the ``github`` service it syncs the watched repos every ``SYNC_INTERVAL`` (the
``issues.sync`` job, also at start) and runs the label batches. It serves ``/api/issues`` and
the read-only ``issues_search`` and ``issues_get`` MCP tools; the cache reads work without
GitHub."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.issues.api.routes import build_router
from jesinia.apps.issues.api.tools import build_tools
from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.sync import SYNC_INTERVAL, SyncService
from jesinia.apps.issues.infrastructure.store import SqliteIssuesStore
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.idempotency import RETENTION, IdempotencyStore, SqliteIdempotencyStore
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.github.application.issues import IssueClient
from jesinia.services.github.application.service import GitHubService

log = logging.getLogger(__name__)

# History: queues, order, notes, themes and drafts are user-authored and can't be rebuilt
# from GitHub, and `jesinia backup` copies only history databases.
ISSUES_DB = ModuleDb("issues", DbClass.HISTORY, files("jesinia.apps.issues.migrations"))
IDEMPOTENCY_PRUNE_JOB = "issues.idempotency-prune"
SYNC_JOB = "issues.sync"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _not_started() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "issues module not started")


class IssuesModule:
    name = "issues"

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._idempotency: SqliteIdempotencyStore | None = None
        self._sync: SyncService | None = None
        self._labels: LabelSetupService | None = None
        self._queries: IssueQueries | None = None

    @property
    def idempotency(self) -> IdempotencyStore:
        if self._idempotency is None:
            raise _not_started()
        return self._idempotency

    def _needs_github(self) -> HTTPException:
        if self._conn is not None:
            return HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "issues require the github service"
            )
        return _not_started()

    @property
    def sync(self) -> SyncService:
        """Watched repos, their sync and the Inbox and Backlog (needs the github service)."""
        if self._sync is None:
            raise self._needs_github()
        return self._sync

    @property
    def labels(self) -> LabelSetupService:
        """The label setup and migration batches (needs the github service)."""
        if self._labels is None:
            raise self._needs_github()
        return self._labels

    def clock(self) -> datetime:
        """The current UTC time (``Retry-After`` is counted from it)."""
        return _utc_now()

    @property
    def queries(self) -> IssueQueries:
        """Cache-only reads: repos, one issue, search."""
        if self._queries is None:
            raise _not_started()
        return self._queries

    async def sync_repos(self) -> None:
        """The ``issues.sync`` job: every watched repo, failures recorded, never raised."""
        if self._sync is not None:
            await self._sync.sync_all()

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys older than ``RETENTION`` (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired issues idempotency keys", removed)

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, ISSUES_DB)
        self._idempotency = SqliteIdempotencyStore(self._conn)
        # Single process and nothing served yet: any pending key was left by a crash.
        if cleared := self._idempotency.clear_pending():
            log.info("cleared %d issues idempotency keys left pending by an unclean stop", cleared)
        ctx.scheduler.every(
            IDEMPOTENCY_PRUNE_JOB, timedelta(days=1), self.prune_idempotency_keys, at_start=True
        )
        store = SqliteIssuesStore(self._conn)
        self._queries = IssueQueries(store, _utc_now)
        github = ctx.services.get("github")
        if isinstance(github, GitHubService):
            client = IssueClient(github)
            self._sync = SyncService(client, store, _utc_now)
            self._queries = IssueQueries(store, _utc_now, self._sync.is_syncing)
            self._labels = LabelSetupService(
                client, store, _utc_now, on_rate_limit=self._sync.back_off
            )
            ctx.scheduler.every(SYNC_JOB, SYNC_INTERVAL, self.sync_repos, at_start=True)

    async def stop(self) -> None:
        self._idempotency = None
        self._sync = None
        self._labels = None
        self._queries = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        ready = self._conn is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
