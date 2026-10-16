# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex runner: ``codex exec --json`` in a read-only sandbox.

Verified against codex-cli 0.163.0: ``exec`` (prompt on stdin via ``-``), ``--json``,
``--sandbox read-only``, ``-C <dir>``, ``--skip-git-repo-check``, ``-m``, ``-c key=value``
config overrides for the MCP server, and ``exec resume <id>``. Codex has no
append-system-prompt flag, so the role prompt leads the first turn's prompt.
"""

from __future__ import annotations

import json

from jesinia.services.agents.application.packs import pack_contents
from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import (
    AgentEvent,
    RunError,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnDone,
)
from jesinia.services.agents.infrastructure.cli import CliRunner, checked_resume_id, minimal_env

TOKEN_ENV = "JESINIA_MCP_TOKEN"


def _mcp_endpoint(req: TurnRequest) -> tuple[str, str]:
    server = json.loads(req.mcp_config.read_text())["mcpServers"]["jesinia"]
    token = str(server.get("headers", {}).get("Authorization", "")).removeprefix("Bearer ")
    return str(server["url"]), token


class CodexStreamParser:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.malformed: str | None = None  # the first unparseable record, if any

    def feed(self, line: str) -> list[AgentEvent]:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            self.malformed = self.malformed or line[:200]
            return []
        if not isinstance(d, dict):
            return []
        kind = d.get("type")
        if kind == "thread.started":
            self.thread_id = str(d.get("thread_id") or "")
            return []
        if kind in ("item.started", "item.completed"):
            item = d.get("item") or {}
            if item.get("type") == "agent_message" and kind == "item.completed":
                return [TextDelta(str(item.get("text", "")))]
            if item.get("type") == "mcp_tool_call":
                name = str(item.get("tool", ""))
                if kind == "item.started":
                    return [ToolUse(name, dict(item.get("arguments") or {}))]
                ok = item.get("status") == "completed" and not item.get("error")
                return [ToolResult(name, ok)]
            return []
        if kind == "turn.completed":
            if self.malformed is not None:
                return [RunError(f"codex sent a malformed record: {self.malformed!r}")]
            return [TurnDone(self.thread_id or "", None)]
        if kind == "turn.failed":
            return [RunError(str((d.get("error") or {}).get("message", "turn failed")))]
        return []


def parse_codex_line(line: str) -> list[AgentEvent]:
    return CodexStreamParser().feed(line)


class CodexRunner(CliRunner):
    name = "codex"
    binary = "codex"

    def build_argv(self, req: TurnRequest) -> list[str]:
        url, _ = _mcp_endpoint(req)
        tools = ", ".join(json.dumps(t) for t in req.allowed_tools)
        argv = [
            self.binary,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            # No shell at all: the read-only sandbox would still let it read any file.
            "--disable",
            "shell_tool",
            "--disable",
            "unified_exec",
            "--skip-git-repo-check",
            "-C",
            str(req.workdir),
            "-c",
            f"mcp_servers.jesinia.url={json.dumps(url)}",
            "-c",
            f'mcp_servers.jesinia.bearer_token_env_var="{TOKEN_ENV}"',
            "-c",
            f"mcp_servers.jesinia.enabled_tools=[{tools}]",
        ]
        if self.model:
            argv += ["-m", self.model]
        if req.resume_id:
            argv += ["resume", checked_resume_id(req.resume_id)]
        argv.append("-")
        return argv

    def build_env(self, req: TurnRequest) -> dict[str, str]:
        _, token = _mcp_endpoint(req)
        return minimal_env({TOKEN_ENV: token})

    def stdin(self, req: TurnRequest) -> str:
        if req.resume_id:
            return req.prompt
        # Codex runs with its shell disabled, so it can't open the pack files itself:
        # the first turn carries them (untrusted ones still fenced).
        pack = pack_contents(req.workdir)
        return "\n\n".join(p for p in (req.system_prompt, pack, req.prompt) if p)

    def parser(self) -> CodexStreamParser:
        return CodexStreamParser()
