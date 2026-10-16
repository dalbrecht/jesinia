# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/mcp``: one authenticated, stateless streamable-HTTP MCP endpoint.

Callers authenticate with ``Authorization: Bearer`` — the install token (terminal agents,
CLI) or a per-session token (agents Jesinia launched). Browser cookies are not accepted.
Write tools from session principals pass through a ``WriteGate`` (plan Task 18).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ValidationError
from starlette.types import Receive, Scope, Send

from jesinia import __version__
from jesinia.core import auth
from jesinia.core.module import AppContext, CallContext, ToolDef

PRINCIPAL_KEY = "jesinia.principal"


@dataclass(frozen=True)
class GateDecision:
    kind: Literal["allow", "pending", "deny"]
    pending_id: str | None = None
    reason: str | None = None


class WriteGate(Protocol):
    async def check(
        self, tool: ToolDef, args: BaseModel, principal: auth.Principal
    ) -> GateDecision: ...


class DefaultGate:
    """The owner may call anything; sessions may not write until the agents service
    installs its confirming gate."""

    async def check(
        self, tool: ToolDef, args: BaseModel, principal: auth.Principal
    ) -> GateDecision:
        if principal.kind == "user" or not tool.writes:
            return GateDecision("allow")
        return GateDecision("deny", reason="writes from agent sessions need user approval")


def _text(value: object, *, error: bool = False) -> types.CallToolResult:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=error)


def _authenticate(scope: Scope, cfg_token: str) -> auth.Principal | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    scheme, _, token = headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    if secrets.compare_digest(token.encode(), cfg_token.encode()):
        return auth.Principal("user")
    session_id = auth.session_for_token(token)
    return auth.Principal("session", session_id) if session_id is not None else None


@dataclass
class McpEndpoint:
    manager: StreamableHTTPSessionManager
    tools: dict[str, ToolDef]
    ctx: AppContext

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        principal = _authenticate(scope, self.ctx.cfg.token)
        if principal is None:
            body = b'{"detail":"authentication required"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope[PRINCIPAL_KEY] = principal
        await self.manager.handle_request(scope, receive, send)


def build_mcp(
    ctx: AppContext,
    tools: Sequence[ToolDef],
    gate: Callable[[], WriteGate] | None = None,
) -> McpEndpoint:
    by_name: dict[str, ToolDef] = {}
    for tool in tools:
        if tool.name in by_name:
            raise ValueError(f"duplicate MCP tool name {tool.name!r}")
        by_name[tool.name] = tool
    gate_for: Callable[[], WriteGate] = gate or (lambda: DefaultGate())

    async def on_list_tools(
        rctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_model.model_json_schema(),
                )
                for t in by_name.values()
            ]
        )

    async def on_call_tool(rctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = by_name.get(params.name)
        if tool is None:
            return _text(f"UnknownTool: {params.name}", error=True)
        request = getattr(rctx, "request", None)
        principal = request.scope.get(PRINCIPAL_KEY) if request is not None else None
        if not isinstance(principal, auth.Principal):
            return _text("Unauthenticated: no principal on the request", error=True)
        try:
            args = tool.input_model.model_validate(params.arguments or {})
        except ValidationError as exc:
            return _text(f"ValidationError: {exc}", error=True)
        decision = await gate_for().check(tool, args, principal)
        if decision.kind == "pending":
            return _text(
                f"awaiting user approval (pending_id={decision.pending_id}). The user will "
                "approve or reject this change in Jesinia; do not retry it."
            )
        if decision.kind == "deny":
            return _text(f"WriteDenied: {decision.reason}", error=True)
        try:
            result = await tool.handler(args, CallContext(principal.session_id))
        except Exception as exc:
            return _text(f"{type(exc).__name__}: {exc}", error=True)
        return _text(result)

    server: Server[Any] = Server(
        "jesinia",
        version=__version__,
        instructions=(
            "Jesinia knowledge base tools. KB pages are Markdown files; read before writing."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
    return McpEndpoint(manager, by_name, ctx)
