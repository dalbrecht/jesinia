# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``fetch`` service: guarded, cached outbound HTTP for apps (spec §4.5)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import timedelta
from importlib.resources import files

from fastapi import APIRouter

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.fetch.application.ports import Resolver
from jesinia.services.fetch.application.service import FetchService, system_resolve
from jesinia.services.fetch.domain.address import is_public
from jesinia.services.fetch.infrastructure.cache import SqliteFetchCache
from jesinia.services.fetch.infrastructure.extract import TrafilaturaExtractor
from jesinia.services.fetch.infrastructure.transport import guarded_transport

FETCH_DB = ModuleDb("fetch", DbClass.CACHE, files("jesinia.services.fetch.migrations"))
CACHE_TTL = timedelta(days=30)


class FetchModule:
    name = "fetch"

    def __init__(
        self, resolve: Resolver = system_resolve, allowed: Callable[[str], bool] = is_public
    ) -> None:
        self._resolve = resolve
        self._allowed = allowed
        self._conn: sqlite3.Connection | None = None

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, FETCH_DB)
        cache = SqliteFetchCache(self._conn)
        cache.prune(CACHE_TTL)
        ctx.services["fetch"] = FetchService(
            cache,
            resolve=self._resolve,
            allowed=self._allowed,
            transport=guarded_transport(self._resolve, self._allowed),
            extractor=TrafilaturaExtractor(),
        )

    async def stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        return ModuleHealth(self._conn is not None, "ready" if self._conn else "not started")
