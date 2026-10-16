# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastAPI app factory: hosts modules, health, auth, and the local-origin guard."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status

from jesinia.core.auth import auth_router, require_owner
from jesinia.core.config import Config
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext, Module, ModuleHealth
from jesinia.core.scheduler import Scheduler
from jesinia.core.security import LocalOriginGuard

log = logging.getLogger(__name__)


def create_app(cfg: Config, modules: Sequence[Module]) -> FastAPI:
    """Build the server app. Modules start in order and stop in reverse; a module whose
    ``start`` raises is reported unhealthy and the rest keep running."""
    ctx = AppContext(cfg=cfg, bus=EventBus(), scheduler=Scheduler())
    failed: dict[str, str] = {}
    started: list[Module] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for module in modules:
            try:
                await module.start(ctx)
                started.append(module)
            except Exception as exc:
                log.exception("module %s failed to start", module.name)
                failed[module.name] = f"start failed: {exc}"
        await ctx.scheduler.start()
        try:
            yield
        finally:
            await ctx.scheduler.stop()
            for module in reversed(started):
                try:
                    await module.stop()
                except Exception:
                    log.exception("module %s failed to stop", module.name)
            started.clear()

    app = FastAPI(title="Jesinia", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.include_router(auth_router())

    def available(name: str):  # type: ignore[no-untyped-def]
        def check() -> None:
            if name in failed:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, f"{name} is unavailable: {failed[name]}"
                )

        return check

    for module in modules:
        router = module.router()
        if router is not None:
            # HTTP routes serve the PWAs (the owner). Agent sessions reach Jesinia only
            # through MCP, where write confirmation applies (spec §4.3), so their session
            # tokens are refused here.
            # A module whose start() failed answers 503 instead of reaching half-initialized
            # handlers.
            app.include_router(
                router, dependencies=[Depends(require_owner), Depends(available(module.name))]
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        report: dict[str, dict[str, object]] = {}
        for module in modules:
            if module.name in failed:
                health = ModuleHealth(False, failed[module.name])
            else:
                health = module.health()
            report[module.name] = {"ok": health.ok, "detail": health.detail}
        return {"ok": all(m["ok"] for m in report.values()), "modules": report}

    app.add_middleware(LocalOriginGuard, port=cfg.port)
    return app
