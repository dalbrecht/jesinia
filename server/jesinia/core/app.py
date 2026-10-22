# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastAPI app factory: hosts modules, health, auth, and the local-origin guard."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from starlette.routing import Route

from jesinia.core.auth import (
    SESSIONS_FILE,
    SessionCookieRenewal,
    auth_router,
    is_authorized,
    load_sessions,
    require_owner,
)
from jesinia.core.config import Config
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext, Module, ModuleHealth
from jesinia.core.scheduler import Scheduler
from jesinia.core.security import LocalOriginGuard
from jesinia.core.static import STATIC, static_router
from jesinia.core.stream import EventLog, events_router

log = logging.getLogger(__name__)


def create_app(
    cfg: Config,
    modules: Sequence[Module],
    *,
    static_root: Path | None = STATIC,
    on_token_rotated: Callable[[Config], None] | None = None,
) -> FastAPI:
    """Build the server app. Modules start in order and stop in reverse; a module whose
    ``start`` raises is reported unhealthy and the rest keep running."""
    from jesinia.mcp.server import DefaultGate, WriteGate, build_mcp  # core hosts /mcp

    ctx = AppContext(cfg=cfg, bus=EventBus(), scheduler=Scheduler())
    if on_token_rotated is not None:
        ctx.services["auth.token_rotated"] = on_token_rotated
    event_log = EventLog(ctx.bus)
    failed: dict[str, str] = {}
    started: list[Module] = []

    def gate() -> WriteGate:
        installed = ctx.services.get("agents.gate")  # set by the agents module (Task 18)
        return installed if installed is not None else DefaultGate()  # type: ignore[return-value]

    mcp = build_mcp(ctx, [t for m in modules for t in m.mcp_tools()], gate)
    ctx.services["mcp.tools"] = mcp.tools  # the agents service executes approved writes

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
            async with mcp.manager.run():
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
    load_sessions(cfg.data_dir / SESSIONS_FILE, cfg.token)
    app.include_router(auth_router())
    app.include_router(events_router(event_log))

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
    async def healthz(request: Request) -> dict[str, object]:
        report: dict[str, dict[str, object]] = {}
        for module in modules:
            if module.name in failed:
                health = ModuleHealth(False, failed[module.name])
            else:
                health = module.health()
            report[module.name] = {"ok": health.ok, "detail": health.detail}
        ok = all(m["ok"] for m in report.values())
        # Liveness is public; which modules fail and why needs a credential. Never decided
        # by Host: through ``tailscale serve`` a client chooses its own (ADR 0011).
        return {"ok": ok, "modules": report if is_authorized(request) else {}}

    app.router.routes.append(Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]))
    if static_root is not None:
        app.include_router(static_router(static_root))  # last: a catch-all for the PWAs
    app.add_middleware(SessionCookieRenewal, remote_host=cfg.remote_host)
    app.add_middleware(LocalOriginGuard, port=cfg.port, remote_host=cfg.remote_host)
    return app
