# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``search`` module. Must start after ``kb``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Sequence
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.kb.application.service import KbService
from jesinia.services.search.api.routes import build_router, build_tools
from jesinia.services.search.application.service import SearchService
from jesinia.services.search.infrastructure.fts import FtsIndex

log = logging.getLogger(__name__)

SEARCH_DB = ModuleDb("search", DbClass.DERIVED, files("jesinia.services.search.migrations"))


class SearchModule:
    name = "search"

    def __init__(self) -> None:
        self._svc: SearchService | None = None
        self._conn: sqlite3.Connection | None = None
        self._task: asyncio.Task[int] | None = None
        self._error: str | None = None

    @property
    def service(self) -> SearchService:
        if self._svc is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "search module not started")
        return self._svc

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        kb = ctx.services.get("kb")
        if not isinstance(kb, KbService):
            raise RuntimeError("search requires the kb service (start kb first)")
        self._conn = open_db(ctx.cfg.data_dir, SEARCH_DB)
        self._svc = SearchService(FtsIndex(self._conn), kb)
        self._svc.subscribe(ctx.bus)
        ctx.services["search"] = self._svc
        self._svc.indexing = True
        self._task = asyncio.create_task(self._svc.reindex(), name="search-reindex")
        self._task.add_done_callback(self._reindexed)

    def _reindexed(self, task: asyncio.Task[int]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("search reindex failed", exc_info=exc)
            self._error = f"reindex failed: {exc}"

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        if self._error is not None:
            return ModuleHealth(False, f"{self._error} (run `jesinia reindex`)")
        if self._svc is not None and self._svc.indexing:
            return ModuleHealth(True, "indexing")
        return ModuleHealth(True, "ready")
