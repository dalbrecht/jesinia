# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server with a scripted ``fake`` runner, for end-to-end tests (plan Task 25).

    JESINIA_HOME=<tmp> JESINIA_FAKE_RUNNER=1 uvicorn jesinia.testing.fake_runner_app:app

On its first turn in a session the runner calls ``kb_write`` through the session's MCP
endpoint, exactly as a real agent would (so the write gate makes it a pending write),
then replies. Later turns only reply. A tag ask (a prompt starting ``TAGS:``) gets a
fixed JSON tag list and never writes. The fetch service may also reach loopback
addresses, so e2e can serve fixture feeds from a local port. It refuses to load outside a
throwaway ``JESINIA_HOME``, so neither can ever activate in a real install.
"""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi import FastAPI

from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.domain.events import (
    AgentEvent,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnDone,
)
from jesinia.services.fetch.domain.address import is_public
from jesinia.services.fetch.module import FetchModule

DRAFT: dict[str, object] = {
    "title": "Agent draft",
    "folder": "Notes",
    "body": "Drafted by the fake runner.",
}
TAG_REPLY = '["fixture","new-fixture-tag"]'
REPLY = (
    "I read the page. ",
    "It is about the fixture topic. ",
    "I drafted a page for you to review.",
)

Post = Callable[[str, dict[str, str], dict[str, Any]], Any]


async def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


class McpScriptRunner:
    name = "fake"

    def __init__(self, post: Post = _post) -> None:
        self.post = post
        self.turns: dict[str, int] = {}

    async def status(self) -> RunnerStatus:
        return RunnerStatus(True, "fake", "scripted test runner")

    async def _call_kb_write(self, req: TurnRequest) -> bool:
        server = json.loads(req.mcp_config.read_text())["mcpServers"]["jesinia"]
        headers = {
            **server["headers"],
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "kb_write", "arguments": DRAFT},
        }
        result = await self.post(server["url"], headers, payload)
        return not bool(result.get("result", {}).get("isError", True))

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        key = str(req.workdir)
        if req.prompt.startswith("TAGS:"):
            yield TextDelta(TAG_REPLY)
            yield TurnDone(f"fake-{key[-8:]}", "fake-model")
            return
        turn = self.turns.get(key, 0)
        self.turns[key] = turn + 1
        if turn == 0:
            yield ToolUse("kb_write", DRAFT)
            yield ToolResult("kb_write", await self._call_kb_write(req))
        for chunk in REPLY if turn == 0 else ("Noted. ", "Anything else?"):
            yield TextDelta(chunk)
        yield TurnDone(f"fake-{key[-8:]}", "fake-model")


def _require_throwaway_home() -> None:
    if os.environ.get("JESINIA_FAKE_RUNNER") != "1" or not os.environ.get("JESINIA_HOME"):
        raise RuntimeError(
            "fake_runner_app is for tests only: "
            "set JESINIA_FAKE_RUNNER=1 and a throwaway JESINIA_HOME"
        )


def allow_loopback(address: str) -> bool:
    """Public addresses, plus loopback (where e2e serves fixture feeds); never private
    networks, link-local or metadata addresses."""
    if is_public(address):
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def fixture_fetch_module() -> FetchModule:
    """The fetch service widened to loopback; only under the throwaway-home guard."""
    _require_throwaway_home()
    return FetchModule(allowed=allow_loopback)


def build_app() -> FastAPI:
    _require_throwaway_home()
    from jesinia.core.app import create_app
    from jesinia.core.config import load_config
    from jesinia.core.module import Module
    from jesinia.core.paths import config_path
    from jesinia.registry import modules
    from jesinia.services.agents.module import AgentsModule

    replacements: dict[str, Callable[[], Module]] = {
        "agents": lambda: AgentsModule(runners={"fake": McpScriptRunner()}),
        "fetch": fixture_fetch_module,
    }
    mods = [replacements[m.name]() if m.name in replacements else m for m in modules()]
    return create_app(load_config(config_path()), mods)


def __getattr__(name: str) -> FastAPI:  # `uvicorn jesinia.testing.fake_runner_app:app`
    if name == "app":
        return build_app()
    raise AttributeError(name)
