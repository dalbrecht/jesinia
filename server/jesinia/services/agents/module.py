# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``agents`` module: runners, sessions, the write gate, and terminal attach."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.idempotency import RETENTION, IdempotencyStore, SqliteIdempotencyStore
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.agents.api.routes import build_router, build_tools
from jesinia.services.agents.application.gate import SessionWriteGate
from jesinia.services.agents.application.runner import AgentRunner
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.agents.application.settings import RunnerModels
from jesinia.services.agents.domain.events import TextDelta, TurnDone
from jesinia.services.agents.infrastructure.claude import ClaudeCodeRunner
from jesinia.services.agents.infrastructure.codex import CodexRunner
from jesinia.services.agents.infrastructure.fake import FakeRunner
from jesinia.services.agents.infrastructure.settings_store import SqliteModelSettingsStore
from jesinia.services.agents.infrastructure.store import SqliteSessionStore

SESSIONS_DB = ModuleDb("sessions", DbClass.HISTORY, files("jesinia.services.agents.migrations"))
IDEMPOTENCY_PRUNE_JOB = "agents.idempotency-prune"
log = logging.getLogger(__name__)


def default_runners(models: RunnerModels | None = None) -> dict[str, AgentRunner]:
    """The real runners. With ``models`` each asks it for its model on every turn (setting,
    then environment, then default); without, they read the environment once."""
    if models is not None:
        claude = ClaudeCodeRunner(model_source=models.source_for("claude-code"))
        codex = CodexRunner(model_source=models.source_for("codex"))
    else:
        claude = ClaudeCodeRunner(model=os.environ.get("JESINIA_CLAUDE_MODEL") or None)
        codex = CodexRunner(model=os.environ.get("JESINIA_CODEX_MODEL") or None)
    runners: dict[str, AgentRunner] = {"claude-code": claude, "codex": codex}
    # The scripted runner exists only for tests/e2e against a throwaway JESINIA_HOME.
    if os.environ.get("JESINIA_FAKE_RUNNER") == "1" and os.environ.get("JESINIA_HOME"):
        runners["fake"] = FakeRunner(
            [TextDelta("Hello from the fake runner."), TurnDone("fake-1", "fake")]
        )
    return runners


class AgentsModule:
    name = "agents"

    def __init__(self, runners: Mapping[str, AgentRunner] | None = None) -> None:
        self._runners = dict(runners) if runners is not None else None
        self._svc: SessionService | None = None
        self._conn: sqlite3.Connection | None = None
        self._status: dict[str, dict[str, object]] = {}
        self._models: RunnerModels | None = None
        self._idempotency: SqliteIdempotencyStore | None = None

    @property
    def service(self) -> SessionService:
        if self._svc is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "agents module not started")
        return self._svc

    @property
    def models(self) -> RunnerModels:
        if self._models is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "agents module not started")
        return self._models

    @property
    def idempotency(self) -> IdempotencyStore:
        if self._idempotency is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "agents module not started")
        return self._idempotency

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys past their retention (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired agents idempotency keys", removed)

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def runner_status(self) -> dict[str, dict[str, object]]:
        assert self._svc is not None
        out: dict[str, dict[str, object]] = {}
        for name, runner in self._svc.runners.items():
            st = await runner.status()
            out[name] = {"available": st.available, "version": st.version, "detail": st.detail}
        self._status = out
        return out

    def preferred_runner(self) -> str | None:
        """The runner for background asks: ``claude-code`` if available, else the first
        available one (as of the last status check), else None."""
        available = [name for name, st in self._status.items() if st.get("available")]
        if "claude-code" in available:
            return "claude-code"
        return available[0] if available else None

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, SESSIONS_DB)
        self._models = RunnerModels(SqliteModelSettingsStore(self._conn), os.environ)
        self._idempotency = SqliteIdempotencyStore(self._conn)
        if cleared := self._idempotency.clear_pending():
            log.info("cleared %d agents idempotency keys left pending by an unclean stop", cleared)
        ctx.scheduler.every(
            IDEMPOTENCY_PRUNE_JOB, timedelta(days=1), self.prune_idempotency_keys, at_start=True
        )
        runners = self._runners if self._runners is not None else default_runners(self._models)

        def tools() -> Mapping[str, ToolDef]:
            registry = ctx.services.get("mcp.tools")
            return registry if isinstance(registry, Mapping) else {}

        self._svc = SessionService(
            SqliteSessionStore(self._conn),
            runners,
            ctx.cfg.data_dir / "sessions",
            f"http://127.0.0.1:{ctx.cfg.port}/mcp",
            tools,
        )
        self._svc.restore_tokens()
        await self._svc.recover()
        ctx.services["agents"] = self._svc
        ctx.services["agents.gate"] = SessionWriteGate(self._svc)
        ctx.services["agents.runner"] = self.preferred_runner
        await self.runner_status()

    async def stop(self) -> None:
        if self._svc is not None:
            await self._svc.stop()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        if not self._status:
            return ModuleHealth(False, "not started")
        available = [n for n, s in self._status.items() if s["available"]]
        detail = ", ".join(
            f"{n} {'ok' if s['available'] else 'unavailable'}" for n, s in self._status.items()
        )
        return ModuleHealth(bool(available), detail)
