# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``github`` service: shared, cached GitHub API access for apps (spec §4.5)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from importlib.resources import files

import httpx
from fastapi import APIRouter

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.github.application.ports import TokenSource
from jesinia.services.github.application.service import GitHubService
from jesinia.services.github.infrastructure.auth import github_token
from jesinia.services.github.infrastructure.cache import SqliteGitHubCache

GITHUB_DB = ModuleDb("github", DbClass.CACHE, files("jesinia.services.github.migrations"))
CACHE_TTL = timedelta(days=30)


class GitHubModule:
    name = "github"

    def __init__(
        self,
        token: TokenSource = github_token,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        # Only the e2e fixture app sets this (to reach its fake GitHub); production never does.
        self._transport = transport
        self._conn: sqlite3.Connection | None = None
        self._has_token: bool | None = None
        self._service: GitHubService | None = None

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, GITHUB_DB)
        cache = SqliteGitHubCache(self._conn)
        cache.prune(CACHE_TTL)
        self._service = GitHubService(self._token, cache, self._transport)
        ctx.services["github"] = self._service
        self._has_token = (await self._token()) is not None

    async def stop(self) -> None:
        if self._service is not None:
            await self._service.aclose()
            self._service = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        if self._has_token is None:
            return ModuleHealth(False, "not started")
        if self._has_token:
            return ModuleHealth(True, "token available")
        return ModuleHealth(True, "no token: run `gh auth login` to enable GitHub features")
