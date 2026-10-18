# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claude Code runner: ``claude -p`` with stream-json output.

Verified against Claude Code 2.1.301: ``-p`` (prompt on stdin), ``--output-format
stream-json`` (requires ``--verbose``), ``--include-partial-messages`` (text deltas),
``--resume <id>``, ``--mcp-config`` + ``--strict-mcp-config``, ``--allowedTools`` /
``--disallowedTools``, ``--append-system-prompt``, ``--model``, ``--tools``,
``--setting-sources``, ``--disable-slash-commands``, ``--no-chrome``.

Isolation: a session reads untrusted text, so it gets no tool beyond Read (scoped to the
session dir) and Jesinia's MCP server. ``--tools Read`` drops every other built-in tool
(Artifact, Cron, Workflow, Skill, ToolSearch, ...); ``--setting-sources ""`` skips the user's
settings files, so their hooks, permission rules and enabled plugins (with the plugins'
skills, agents and hooks) never load; ``--strict-mcp-config`` keeps only our MCP server.
``--safe-mode`` and ``--bare`` are not usable: safe mode also drops the ``--mcp-config``
server, and bare mode refuses OAuth logins.
"""

from __future__ import annotations

import json
from pathlib import Path

from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import (
    AgentEvent,
    RunError,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnDone,
)
from jesinia.services.agents.infrastructure.cli import CliRunner, checked_resume_id

MCP_PREFIX = "mcp__jesinia__"
# Shell, file-writing, search-anywhere, and network tools stay off. Reading is allowed only
# inside the session scratch dir (the context pack), via a path-scoped Read rule.
DISALLOWED = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Glob",
    "Grep",
)


def read_rule(workdir: Path) -> str:
    """``Read(//abs/dir/**)``: Claude Code's absolute-path permission rule syntax."""
    return f"Read(//{str(workdir.resolve()).lstrip('/')}/**)"


def _tool_name(raw: str) -> str:
    return raw.removeprefix(MCP_PREFIX)


class ClaudeStreamParser:
    def __init__(self) -> None:
        self.model: str | None = None
        self.session_id: str | None = None
        self._tool_names: dict[str, str] = {}
        self.malformed: str | None = None  # the first unparseable record, if any

    def feed(self, line: str) -> list[AgentEvent]:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            # A truncated or garbled record: the transcript may be incomplete, so the
            # turn must not finish as a success even if a valid result follows.
            self.malformed = self.malformed or line[:200]
            return []
        if not isinstance(d, dict):
            return []
        self.session_id = d.get("session_id") or self.session_id
        kind = d.get("type")
        if kind == "system" and d.get("subtype") == "init":
            self.model = d.get("model") or self.model
            return []
        if kind == "stream_event":
            delta = (d.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [TextDelta(str(delta["text"]))]
            return []
        if kind == "assistant":
            out: list[AgentEvent] = []
            for block in (d.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = _tool_name(str(block.get("name", "")))
                    self._tool_names[str(block.get("id"))] = name
                    out.append(ToolUse(name, dict(block.get("input") or {})))
            return out
        if kind == "user":
            out = []
            for block in (d.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name = self._tool_names.get(str(block.get("tool_use_id")), "unknown")
                    out.append(ToolResult(name, not block.get("is_error", False)))
            return out
        if kind == "result":
            if d.get("is_error") or d.get("subtype") != "success":
                return [RunError(str(d.get("result") or d.get("subtype") or "agent run failed"))]
            if self.malformed is not None:
                return [RunError(f"claude sent a malformed record: {self.malformed!r}")]
            return [TurnDone(str(self.session_id or ""), self.model)]
        return []


def parse_claude_line(line: str) -> list[AgentEvent]:
    """Parse one stream-json line in isolation (tool results need a stateful parser)."""
    return ClaudeStreamParser().feed(line)


class ClaudeCodeRunner(CliRunner):
    name = "claude-code"
    binary = "claude"

    def build_argv(self, req: TurnRequest) -> list[str]:
        allowed = [read_rule(req.workdir), *(MCP_PREFIX + t for t in req.allowed_tools)]
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--strict-mcp-config",
            "--mcp-config",
            str(req.mcp_config),
            "--tools",
            "Read",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-chrome",
            "--allowedTools",
            ",".join(allowed),
            "--disallowedTools",
            ",".join(DISALLOWED),
            "--append-system-prompt",
            req.system_prompt,
        ]
        if self.model:
            argv += ["--model", self.model]
        if req.resume_id:
            argv += ["--resume", checked_resume_id(req.resume_id)]
        return argv

    def parser(self) -> ClaudeStreamParser:
        return ClaudeStreamParser()
