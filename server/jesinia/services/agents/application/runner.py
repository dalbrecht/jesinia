# SPDX-License-Identifier: AGPL-3.0-or-later
"""The agent runner contract: one turn of a headless CLI agent (spec §4.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jesinia.services.agents.domain.events import AgentEvent


@dataclass(frozen=True)
class TurnRequest:
    prompt: str
    workdir: Path  # the session scratch dir (context pack lives here)
    system_prompt: str
    resume_id: str | None
    mcp_config: Path  # Claude-style JSON pointing at Jesinia's /mcp with a session token
    allowed_tools: Sequence[str] = field(default_factory=list)  # Jesinia MCP wire names


@dataclass(frozen=True)
class RunnerStatus:
    available: bool
    version: str | None
    detail: str


class AgentRunner(Protocol):
    name: str

    async def status(self) -> RunnerStatus: ...

    def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]: ...
