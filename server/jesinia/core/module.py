# SPDX-License-Identifier: AGPL-3.0-or-later
"""The module contract every service and app implements (spec §3.3)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel

from jesinia.core.config import Config
from jesinia.core.events import EventBus
from jesinia.core.scheduler import Scheduler

_WIRE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass
class AppContext:
    cfg: Config
    bus: EventBus
    scheduler: Scheduler
    services: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleHealth:
    ok: bool
    detail: str


@dataclass(frozen=True)
class CallContext:
    """Who is calling an MCP tool: a PWA-started agent session, or a terminal agent."""

    session_id: str | None


ToolHandler = Callable[[BaseModel, CallContext], Awaitable[object]]
# What a write would really do, for the approval card: e.g. {"action": "update",
# "path": "Notes/a.md", "current": "<body now>"}.
ToolPreview = Callable[[BaseModel], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class ToolDef:
    """An MCP tool a module offers; mounted by the MCP endpoint (plan Task 16).

    ``name`` is the wire name (``kb_search``); ``writes`` tools need PWA confirmation in
    Jesinia-launched sessions unless pre-authorized (spec §4.3).
    """

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    writes: bool
    preview: ToolPreview | None = None

    def __post_init__(self) -> None:
        if not _WIRE_NAME.match(self.name):
            raise ValueError(
                f"invalid MCP tool name {self.name!r} (must match {_WIRE_NAME.pattern})"
            )


class Module(Protocol):
    name: str

    def router(self) -> APIRouter | None: ...

    def mcp_tools(self) -> Sequence[ToolDef]: ...

    async def start(self, ctx: AppContext) -> None: ...

    async def stop(self) -> None: ...

    def health(self) -> ModuleHealth: ...
