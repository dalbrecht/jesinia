# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Notes app module. Starts after kb, search and agents."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.notes.api import build_router, build_tools
from jesinia.apps.notes.application.service import NotesService
from jesinia.apps.notes.infrastructure.store import SqliteNoteLog
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.kb.application.service import KbService
from jesinia.services.search.application.service import SearchService

NOTES_DB = ModuleDb("notes", DbClass.DERIVED, files("jesinia.apps.notes.migrations"))


class NotesModule:
    name = "notes"

    def __init__(self) -> None:
        self._svc: NotesService | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def service(self) -> NotesService:
        if self._svc is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "notes module not started")
        return self._svc

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        kb, sessions = ctx.services.get("kb"), ctx.services.get("agents")
        if not isinstance(kb, KbService) or not isinstance(sessions, SessionService):
            raise RuntimeError("notes requires the kb and agents services (start them first)")
        search = ctx.services.get("search")
        self._conn = open_db(ctx.cfg.data_dir, NOTES_DB)
        self._svc = NotesService(
            kb,
            search if isinstance(search, SearchService) else None,
            sessions,
            SqliteNoteLog(self._conn),
            lambda: ctx.cfg.folders.get("note", "Notes"),
        )

    async def stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        return ModuleHealth(self._svc is not None, "ready" if self._svc else "not started")
