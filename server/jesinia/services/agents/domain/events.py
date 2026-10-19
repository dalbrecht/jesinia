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
    # The turn's last assistant message: its reply. A turn may say several things (a
    # preamble before its tool calls, then the answer); only this one answers the prompt.
    # None when the runner can't tell its messages apart.
    final_message: str | None = None


@dataclass(frozen=True)
class RunError:
    message: str


AgentEvent = TextDelta | ToolUse | ToolResult | TurnDone | RunError
