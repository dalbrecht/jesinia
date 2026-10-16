# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an agent run emits, independent of which CLI produced it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolUse:
    name: str  # Jesinia wire name (``kb_search``), MCP server prefix removed
    input: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool


@dataclass(frozen=True)
class TurnDone:
    agent_session_id: str  # the CLI's own session/thread id, used to resume
    model: str | None


@dataclass(frozen=True)
class RunError:
    message: str


AgentEvent = TextDelta | ToolUse | ToolResult | TurnDone | RunError
