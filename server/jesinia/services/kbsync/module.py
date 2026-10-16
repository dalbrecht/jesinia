# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``kbsync`` module: periodic KB git sync. Start after ``kb``."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from jesinia.core.events import Event
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.kbsync.api.routes import build_router, build_tools
from jesinia.services.kbsync.application.service import SyncService
from jesinia.services.kbsync.infrastructure.git import GitRunner

log = logging.getLogger(__name__)

TICK = timedelta(minutes=1)


def _git_for(root: Path | None) -> GitRunner | None:
    return GitRunner(root) if root is not None and root.is_dir() else None


class KbSyncModule:
    name = "kbsync"

    def __init__(self) -> None:
        self._svc: SyncService | None = None

    @property
    def service(self) -> SyncService:
        if self._svc is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kbsync module not started")
        return self._svc

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        activity = ctx.services.get("kb.activity")
        probe: Callable[[], datetime | None] = activity if callable(activity) else lambda: None
        root = ctx.cfg.kb_path
        self._svc = SyncService(
            root=root,
            git=_git_for(root),
            interval=ctx.cfg.kbsync_interval,
            quiet=ctx.cfg.kbsync_quiet,
            bus=ctx.bus,
            activity=probe,
        )
        ctx.services["kbsync"] = self._svc
        await self._svc.refresh()
        ctx.scheduler.every("kbsync", TICK, self._tick)
        ctx.bus.subscribe("kb.root_changed", self._on_root_changed)

    async def _tick(self) -> None:
        await self.service.tick()

    async def _on_root_changed(self, e: Event) -> None:
        root = Path(str(e.payload["path"]))
        self.service.configure(root, _git_for(root))
        await self.service.refresh()

    async def stop(self) -> None:
        if self._svc is not None:
            try:
                await self._svc.run_cycle("shutdown")
            except Exception:
                log.exception("final KB sync at shutdown failed")

    def health(self) -> ModuleHealth:
        if self._svc is None:
            return ModuleHealth(False, "not started")
        st = self._svc.status()
        if st.paused:
            return ModuleHealth(False, "sync paused: merge conflict")
        if st.state.mode == "off":
            return ModuleHealth(True, "sync off")
        if st.state.problem_text:
            return ModuleHealth(st.state.problem == "no-upstream", st.state.problem_text)
        return ModuleHealth(True, f"mode {st.state.mode}")
