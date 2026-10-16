# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from jesinia.core.app import create_app
from jesinia.core.auth import register_session_token
from jesinia.core.config import Config
from jesinia.core.module import CallContext, ModuleHealth, ToolDef
from jesinia.mcp.server import DefaultGate, GateDecision, build_mcp
from jesinia.services.kb.module import KbModule
from jesinia.services.kbsync.module import KbSyncModule
from jesinia.services.search.module import SearchModule
from tests.conftest import BASE_URL, running


@asynccontextmanager
async def mcp_session(app: FastAPI, token: str) -> AsyncIterator[ClientSession]:
    transport = httpx2.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL, headers=headers) as http:
        async with streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.fixture
def full_app(cfg: Config, tmp_path: Path) -> tuple[FastAPI, Config]:
    root = tmp_path / "kb"
    root.mkdir()
    c = replace(cfg, kb_path=root)
    return create_app(c, [KbModule(), SearchModule(), KbSyncModule()]), c


def _text(result) -> str:  # type: ignore[no-untyped-def]
    return "".join(getattr(b, "text", "") for b in result.content)


async def test_lists_namespaced_tools(full_app: tuple[FastAPI, Config]) -> None:
    app, cfg = full_app
    async with running(app), mcp_session(app, cfg.token) as s:
        names = {t.name for t in (await s.list_tools()).tools}
    assert {"kb_read", "kb_write", "search_query", "kb_search", "kb_sync_status"} <= names
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in names)


async def test_rejects_missing_bearer(full_app: tuple[FastAPI, Config]) -> None:
    app, cfg = full_app
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as raw:
            assert (await raw.post("/mcp", json={})).status_code == 401
            r = await raw.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
            assert r.status_code == 401
            r = await raw.post("/mcp", json={}, headers={"Authorization": "Basic x"})
            assert r.status_code == 401


async def test_kb_write_then_read_round_trip(full_app: tuple[FastAPI, Config]) -> None:
    app, cfg = full_app
    async with running(app), mcp_session(app, cfg.token) as s:
        made = await s.call_tool(
            "kb_write", {"title": "Via MCP", "folder": "Notes", "body": "hello"}
        )
        assert not made.is_error, _text(made)
        page = json.loads(_text(made))
        read = await s.call_tool("kb_read", {"id": page["id"]})
        assert json.loads(_text(read))["body"] == "hello"


async def test_stale_write_surfaces_as_tool_error(full_app: tuple[FastAPI, Config]) -> None:
    app, cfg = full_app
    async with running(app), mcp_session(app, cfg.token) as s:
        page = json.loads(
            _text(await s.call_tool("kb_write", {"title": "S", "folder": "Notes", "body": "1"}))
        )
        stale = await s.call_tool(
            "kb_write",
            {"id": page["id"], "title": "S", "folder": "Notes", "body": "2", "expected_rev": "old"},
        )
        assert stale.is_error and "StaleWrite" in _text(stale)
        bad = await s.call_tool("kb_read", {})
        assert bad.is_error and "ValidationError" in _text(bad)
        unknown = await s.call_tool("nope", {})
        assert unknown.is_error and "UnknownTool" in _text(unknown)


async def test_session_principal_cannot_write_without_gate(
    full_app: tuple[FastAPI, Config],
) -> None:
    app, _ = full_app
    register_session_token("sess-tok", "s-1")
    async with running(app), mcp_session(app, "sess-tok") as s:
        denied = await s.call_tool("kb_write", {"title": "X", "folder": "Notes", "body": "x"})
        assert denied.is_error and "WriteDenied" in _text(denied)
        tree = await s.call_tool("kb_tree", {})
        assert not tree.is_error


class In(BaseModel):
    pass


async def _noop(args: BaseModel, call: CallContext) -> object:
    return {"session": call.session_id}


class ToolModule:
    name = "dup"

    def router(self):  # type: ignore[no-untyped-def]
        return None

    def mcp_tools(self) -> list[ToolDef]:
        return [ToolDef("same", "d", In, _noop, False), ToolDef("same", "d", In, _noop, False)]

    async def start(self, ctx) -> None:  # type: ignore[no-untyped-def]
        return None

    async def stop(self) -> None:
        return None

    def health(self) -> ModuleHealth:
        return ModuleHealth(True, "ok")


def test_duplicate_tool_names_raise(cfg: Config) -> None:
    with pytest.raises(ValueError, match="duplicate MCP tool"):
        create_app(cfg, [ToolModule()])


async def test_installed_gate_is_used_and_pending_message(cfg: Config) -> None:
    class Pending:
        async def check(self, tool, args, principal):  # type: ignore[no-untyped-def]
            return GateDecision("pending", pending_id="p-9")

    class WriteModule(ToolModule):
        name = "w"

        def mcp_tools(self) -> list[ToolDef]:
            return [ToolDef("w_write", "d", In, _noop, True)]

        async def start(self, ctx) -> None:  # type: ignore[no-untyped-def]
            ctx.services["agents.gate"] = Pending()

    app = create_app(cfg, [WriteModule()])
    async with running(app), mcp_session(app, cfg.token) as s:
        r = await s.call_tool("w_write", {})
        assert not r.is_error and "awaiting user approval (pending_id=p-9)" in _text(r)


async def test_default_gate() -> None:
    from jesinia.core.auth import Principal

    tool = ToolDef("x_write", "d", In, _noop, True)
    read = ToolDef("x_read", "d", In, _noop, False)
    g = DefaultGate()
    assert (await g.check(tool, In(), Principal("user"))).kind == "allow"
    assert (await g.check(read, In(), Principal("session", "s"))).kind == "allow"
    assert (await g.check(tool, In(), Principal("session", "s"))).kind == "deny"


def test_build_mcp_directly(cfg: Config) -> None:
    from jesinia.core.events import EventBus
    from jesinia.core.module import AppContext
    from jesinia.core.scheduler import Scheduler

    ctx = AppContext(cfg, EventBus(), Scheduler())
    ep = build_mcp(ctx, [ToolDef("a", "d", In, _noop, False)])
    assert set(ep.tools) == {"a"}
