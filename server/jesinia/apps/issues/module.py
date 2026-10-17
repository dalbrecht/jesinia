# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Issues app module: owns ``issues.sqlite`` and its idempotency keys. Starts after
``kb`` (tags), ``agents`` and ``github``, which triage, suggestions and writes use.

With the ``github`` service it syncs the watched repos every ``SYNC_INTERVAL`` (the
``issues.sync`` job, also at start) and runs the label batches. Issues entering the Inbox, or
edited while there, are queued for suggestion runs, which the ``issues.suggest`` job starts
through the ``agents`` service (looked up per run). Approvals and the pending-changes tray
apply through one engine (``ApplyEngine``). It serves ``/api/issues`` and the read-only
``issues_search`` and ``issues_get`` MCP tools; the cache reads, suggestion drafts and the tray
work without GitHub."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.issues.api.routes import build_router
from jesinia.apps.issues.api.tools import build_tools
from jesinia.apps.issues.application.apply import ApplyEngine
from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.pending import ApplyService, PendingService
from jesinia.apps.issues.application.ports import AgentAsker
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.suggest import RUN_INTERVAL, SuggestService
from jesinia.apps.issues.application.sync import SYNC_INTERVAL, SyncService
from jesinia.apps.issues.infrastructure.apply_store import SqliteApplyStore
from jesinia.apps.issues.infrastructure.store import SqliteIssuesStore
from jesinia.apps.issues.infrastructure.suggestion_store import SqliteSuggestionStore
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
SUGGEST_JOB = "issues.suggest"
# How long an applied batch stays retryable.
BATCH_RETENTION = timedelta(days=30)


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
        self._suggest: SuggestService | None = None
        self._pending: PendingService | None = None
        self._apply: ApplyService | None = None
        self._applied: SqliteApplyStore | None = None

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

    @property
    def suggest(self) -> SuggestService:
        """Suggestion drafts and their runs (the drafts need no GitHub)."""
        if self._suggest is None:
            raise _not_started()
        return self._suggest

    @property
    def pending(self) -> PendingService:
        """The pending-changes tray (no GitHub needed)."""
        if self._pending is None:
            raise _not_started()
        return self._pending

    @property
    def apply(self) -> ApplyService:
        """Applying the tray and approvals (needs the github service)."""
        if self._apply is None:
            raise self._needs_github()
        return self._apply

    async def run_suggestions(self) -> None:
        """The ``issues.suggest`` job: start the suggestion runs that are due."""
        if self._suggest is not None:
            await self._suggest.run_due()

    @staticmethod
    def _suggestions(
        ctx: AppContext, store: SqliteIssuesStore, conn: sqlite3.Connection
    ) -> SuggestService:
        """The suggestion service. The agents service and its runner choice are looked up per
        run, so suggestions wait (and say so) while no runner is available."""

        def asker() -> AgentAsker | None:
            found = ctx.services.get("agents")
            return found if isinstance(found, AgentAsker) else None

        def runner() -> str | None:
            pick = ctx.services.get("agents.runner")
            chosen = pick() if callable(pick) else None
            return chosen if isinstance(chosen, str) else None

        drafts = SqliteSuggestionStore(conn)
        # Nothing applies yet: a claim was left by an approval a stop cut off (IR40).
        if released := drafts.release_all():
            log.info("released %d issues suggestion claims after an unclean stop", released)
        # Single process and nothing runs yet: a run marked running was cut off by a stop.
        if requeued := drafts.requeue_running(_utc_now()):
            log.info("queued %d issues suggestion runs again after an unclean stop", requeued)
        return SuggestService(drafts, store, asker, runner, _utc_now)

    async def sync_repos(self) -> None:
        """The ``issues.sync`` job: every watched repo, failures recorded, never raised."""
        if self._sync is not None:
            await self._sync.sync_all()

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys older than ``RETENTION`` (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired issues idempotency keys", removed)
        if self._applied is not None:
            gone = self._applied.prune_batches(datetime.now(UTC) - BATCH_RETENTION)
            log.info("pruned %d applied issues batches", gone)

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
        suggest = self._suggest = self._suggestions(ctx, store, self._conn)
        applied = self._applied = SqliteApplyStore(self._conn)
        self._pending = PendingService(applied, store, _utc_now)
        ctx.scheduler.every(SUGGEST_JOB, RUN_INTERVAL, self.run_suggestions, at_start=True)
        github = ctx.services.get("github")
        if isinstance(github, GitHubService):
            client = IssueClient(github)
            self._sync = SyncService(
                client,
                store,
                _utc_now,
                on_inbox=suggest.queue_new,
                on_inbox_edit=suggest.queue_edited,
                on_triaged=suggest.retire_stale,
            )
            self._queries = IssueQueries(store, _utc_now, self._sync.is_syncing)
            self._labels = LabelSetupService(
                client, store, _utc_now, on_rate_limit=self._sync.back_off
            )
            engine = ApplyEngine(
                client,
                store,
                _utc_now,
                on_rate_limit=self._sync.back_off,
                on_schema_changed=self._sync.recompute,
                on_triaged=suggest.retire_stale,
                blocked_until=self._sync.backoff_until,
            )
            self._apply = ApplyService(
                engine, applied, SqliteSuggestionStore(self._conn), store, _utc_now
            )
            ctx.scheduler.every(SYNC_JOB, SYNC_INTERVAL, self.sync_repos, at_start=True)

    async def stop(self) -> None:
        if self._suggest is not None:
            await self._suggest.stop()
            self._suggest = None
        self._idempotency = None
        self._sync = None
        self._labels = None
        self._queries = None
        self._pending = None
        self._apply = None
        self._applied = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        ready = self._conn is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
