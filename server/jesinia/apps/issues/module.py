# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Issues app module: owns ``issues.sqlite`` and its idempotency keys. Starts after
``kb`` (tags), ``agents`` and ``github``, which triage, suggestions and writes use.

A skeleton for now: routes, tools and the GitHub sync arrive with the later tasks."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.idempotency import RETENTION, IdempotencyStore, SqliteIdempotencyStore
from jesinia.core.module import AppContext, ModuleHealth, ToolDef

log = logging.getLogger(__name__)

# History: queues, order, notes, themes and drafts are user-authored and can't be rebuilt
# from GitHub, and `jesinia backup` copies only history databases.
ISSUES_DB = ModuleDb("issues", DbClass.HISTORY, files("jesinia.apps.issues.migrations"))
IDEMPOTENCY_PRUNE_JOB = "issues.idempotency-prune"


def _not_started() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "issues module not started")


class IssuesModule:
    name = "issues"

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._idempotency: SqliteIdempotencyStore | None = None

    @property
    def idempotency(self) -> IdempotencyStore:
        if self._idempotency is None:
            raise _not_started()
        return self._idempotency

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys older than ``RETENTION`` (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired issues idempotency keys", removed)

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, ISSUES_DB)
        self._idempotency = SqliteIdempotencyStore(self._conn)
        # Single process and nothing served yet: any pending key was left by a crash.
        if cleared := self._idempotency.clear_pending():
            log.info("cleared %d issues idempotency keys left pending by an unclean stop", cleared)
        ctx.scheduler.every(
            IDEMPOTENCY_PRUNE_JOB, timedelta(days=1), self.prune_idempotency_keys, at_start=True
        )

    async def stop(self) -> None:
        self._idempotency = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        ready = self._conn is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
