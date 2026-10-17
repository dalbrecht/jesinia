# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``kb`` module: wires the service, index, watcher, routes, and MCP tools."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from jesinia.core.config import set_value
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.events import Event, EventBus
from jesinia.core.idempotency import RETENTION, IdempotencyStore, SqliteIdempotencyStore
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.kb.api.routes import build_router, build_tools
from jesinia.services.kb.api.tags import build_tags_router, build_tags_tools
from jesinia.services.kb.application.scan import ScanReport, scan
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.infrastructure.agent_advisor import AgentTagAdvisor, OneShotAsker
from jesinia.services.kb.infrastructure.fs import LocalFiles
from jesinia.services.kb.infrastructure.index import SqliteIndex
from jesinia.services.kb.infrastructure.watcher import KbWatcher

log = logging.getLogger(__name__)

KB_DB = ModuleDb("kb", DbClass.DERIVED, files("jesinia.services.kb.migrations"))
IDEMPOTENCY_PRUNE_JOB = "kb.idempotency-prune"


def _report(r: ScanReport) -> dict[str, object]:
    return {
        "indexed": r.indexed,
        "adopted": [str(p) for p in r.adopted],
        "missing": list(r.missing),
        "duplicates": [{"id": d.id, "paths": [str(p) for p in d.paths]} for d in r.duplicates],
        "foreign_skipped": r.foreign_skipped,
    }


class KbModule:
    name = "kb"

    def __init__(self) -> None:
        self._svc: KbService | None = None
        self._tags: TagService | None = None
        self._idempotency: SqliteIdempotencyStore | None = None
        self._conn: sqlite3.Connection | None = None
        self._ctx: AppContext | None = None
        self._watcher: KbWatcher | None = None
        self._last: ScanReport | None = None
        self._problem: str | None = None

    @property
    def service(self) -> KbService:
        if self._svc is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kb module not started")
        return self._svc

    @property
    def tags(self) -> TagService:
        if self._tags is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kb module not started")
        return self._tags

    @property
    def idempotency(self) -> IdempotencyStore:
        if self._idempotency is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kb module not started")
        return self._idempotency

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys older than ``RETENTION`` (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired idempotency keys", removed)

    @property
    def watcher(self) -> KbWatcher | None:
        return self._watcher

    def router(self) -> APIRouter | None:
        router = build_router(self)
        router.include_router(build_tags_router(self))
        return router

    def mcp_tools(self) -> Sequence[ToolDef]:
        return [*build_tools(self), *build_tags_tools(self)]

    @staticmethod
    def _usable(path: Path | None) -> str | None:
        if path is None:
            return "no KB path configured"
        if not path.is_dir():
            return f"KB folder not found: {path}"
        if not os.access(path, os.W_OK):
            return f"KB folder is not writable: {path}"
        return None

    async def _attach(self, root: Path | None) -> None:
        assert self._svc is not None
        if self._watcher is not None:
            await self._watcher.stop()
            self._watcher = None
        self._problem = self._usable(root)
        if self._problem is not None or root is None:
            self._svc.set_files(None, self._problem)
            return
        # A different KB from the one indexed (now, or while the server was stopped):
        # its pages are not "missing" — start the derived index over.
        resolved = str(root.resolve())
        if self._svc.index.indexed_root() != resolved:
            self._svc.index.reset()
            self._svc.index.set_indexed_root(resolved)
        self._svc.set_files(LocalFiles(root))
        self._last = await scan(self._svc)
        self._watcher = KbWatcher(root, self._on_paths)
        await self._watcher.start()

    async def _on_paths(self, paths: set[Path]) -> None:
        if self._svc is not None:
            self._last = await scan(self._svc)

    @staticmethod
    def _advisor(ctx: AppContext) -> AgentTagAdvisor:
        """The agent-backed advisor. The agents module starts after kb, so its service
        and runner choice are looked up per ask; without them answers are catalog-only."""

        def sessions() -> OneShotAsker | None:
            found = ctx.services.get("agents")
            return found if isinstance(found, OneShotAsker) else None

        def runner() -> str | None:
            pick = ctx.services.get("agents.runner")
            chosen = pick() if callable(pick) else None
            return chosen if isinstance(chosen, str) else None

        return AgentTagAdvisor(sessions, runner)

    async def start(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._conn = open_db(ctx.cfg.data_dir, KB_DB)
        self._idempotency = SqliteIdempotencyStore(self._conn)
        # Single process and nothing served yet: any pending key was left by a crash.
        if cleared := self._idempotency.clear_pending():
            log.info("cleared %d idempotency keys left pending by an unclean stop", cleared)
        ctx.scheduler.every(IDEMPOTENCY_PRUNE_JOB, timedelta(days=1), self.prune_idempotency_keys)
        self._svc = KbService(None, SqliteIndex(self._conn), ctx.bus)
        ctx.services["kb"] = self._svc
        self._tags = TagService(self._svc, self._advisor(ctx))
        ctx.services["tags"] = self._tags
        # kbsync's quiet period reads the watcher's last activity (spec §4.4 step 2).
        ctx.services["kb.activity"] = lambda: (
            self._watcher.last_activity if self._watcher is not None else None
        )
        await self._attach(ctx.cfg.kb_path)

    async def stop(self) -> None:
        if self._watcher is not None:
            await self._watcher.stop()
            self._watcher = None
        self._idempotency = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        if self._problem is not None:
            return ModuleHealth(False, self._problem)
        pages = self._last.indexed if self._last else 0
        return ModuleHealth(True, f"{pages} pages indexed")

    async def rescan(self) -> dict[str, object]:
        self._last = await scan(self.service)
        return _report(self._last)

    def root_info(self) -> dict[str, object]:
        path = self._ctx.cfg.kb_path if self._ctx is not None else None
        return {
            "path": str(path) if path else None,
            "problem": self._problem,
            "pages": self._last.indexed if self._last else 0,
        }

    async def change_root(self, path: str) -> dict[str, object]:
        assert self._ctx is not None
        root = Path(path).expanduser()
        problem = self._usable(root) if root.is_absolute() else "the KB path must be absolute"
        if problem is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
        set_value(self._ctx.cfg.data_dir / "config.toml", "kb.path", str(root))
        # Only kb_path changes in memory; never swap other live settings (e.g. the token).
        self._ctx.cfg = replace(self._ctx.cfg, kb_path=root)
        await self._attach(root)
        bus: EventBus = self._ctx.bus
        await bus.publish(Event("kb.root_changed", {"path": str(root)}))
        return {"path": str(root), **(_report(self._last) if self._last else {})}
