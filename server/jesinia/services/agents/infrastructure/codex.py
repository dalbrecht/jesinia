# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex runner: ``codex exec --json`` in a read-only sandbox.

Verified against codex-cli 0.163.0: ``exec`` (prompt on stdin via ``-``), ``--json``,
``--sandbox read-only``, ``-C <dir>``, ``--skip-git-repo-check``, ``-m``, ``-c key=value``
config overrides for the MCP server, ``--ignore-user-config``, ``--ignore-rules``,
``--disable <feature>``, and ``exec resume <id>``. Codex has no append-system-prompt flag, so
the role prompt leads the first turn's prompt.

Isolation: a session reads untrusted text (PR bodies, KB pages), so it gets no tool beyond
Jesinia's MCP server. ``--ignore-user-config`` drops the user's own MCP servers and enabled
plugins from ``$CODEX_HOME/config.toml`` (auth still comes from ``CODEX_HOME``), but ChatGPT
apps/connectors, the plugin marketplace, browser/computer use, sub-agents, web search and
skills come from elsewhere, so each is switched off explicitly (see ``ISOLATION_ARGS``).
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

# Optional tool sources that are not (only) configured in config.toml. Each was observed
# adding model-visible tools in codex-cli 0.163.0 under --ignore-user-config: ``apps``
# (Gmail, Drive, Calendar, GitHub... connectors via tool_search), ``plugins`` /
# ``remote_plugin`` / ``tool_suggest`` (plugin install), ``multi_agent`` (spawn_agent),
# ``goals``, ``sleep_tool``, ``view_image``, web search. The rest are switched off as defence
# in depth.
_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "tool_suggest",
    "computer_use",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "multi_agent",
    "goals",
    "sleep_tool",
    "image_generation",
    "view_image",
    "memories",
    "hooks",
    # No shell at all: the read-only sandbox would still let it read any file.
    "shell_tool",
    "unified_exec",
)
ISOLATION_ARGS: tuple[str, ...] = (
    "--ignore-user-config",
    "--ignore-rules",
    *(arg for feature in _DISABLED_FEATURES for arg in ("--disable", feature)),
    "-c",
    'web_search="disabled"',
    # Skill and AGENTS.md instructions would point the agent at the user's local files.
    "-c",
    "skills.include_instructions=false",
    "-c",
    "project_doc_max_bytes=0",
)


def _mcp_endpoint(req: TurnRequest) -> tuple[str, str] | None:
    """The Jesinia MCP url and token, or None when the session has no MCP server (a
    tool-less one-shot session)."""
    server = json.loads(req.mcp_config.read_text()).get("mcpServers", {}).get("jesinia")
    if not isinstance(server, dict):
        return None
    token = str(server.get("headers", {}).get("Authorization", "")).removeprefix("Bearer ")
    return str(server["url"]), token


class CodexStreamParser:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.malformed: str | None = None  # the first unparseable record, if any
        # Each completed agent_message item is a whole message of its own (a preamble
        # before the tool calls is one); the last one is the turn's reply.
        self.last_message: str | None = None

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
                self.last_message = str(item.get("text", ""))
                return [TextDelta(self.last_message)]
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
            return [TurnDone(self.thread_id or "", None, self.last_message)]
        if kind == "turn.failed":
            return [RunError(str((d.get("error") or {}).get("message", "turn failed")))]
        return []


def parse_codex_line(line: str) -> list[AgentEvent]:
    return CodexStreamParser().feed(line)


class CodexRunner(CliRunner):
    name = "codex"
    binary = "codex"

    def build_argv(self, req: TurnRequest) -> list[str]:
        endpoint = _mcp_endpoint(req)
        # --sandbox and -C belong to `exec` only. Everything else goes after the `resume`
        # subcommand when there is one: codex-cli 0.163.0 silently drops -c / --disable
        # given before `resume`, which re-exposed web search and lost the MCP server.
        argv = [self.binary, "exec", "--sandbox", "read-only", "-C", str(req.workdir)]
        if req.resume_id:
            argv.append("resume")
        argv += ["--json", *ISOLATION_ARGS, "--skip-git-repo-check"]
        if endpoint is not None:
            tools = ", ".join(json.dumps(t) for t in req.allowed_tools)
            argv += [
                "-c",
                f"mcp_servers.jesinia.url={json.dumps(endpoint[0])}",
                "-c",
                f'mcp_servers.jesinia.bearer_token_env_var="{TOKEN_ENV}"',
                "-c",
                f"mcp_servers.jesinia.enabled_tools=[{tools}]",
                # exec never prompts, so tools needing approval would be auto-denied. The
                # tool list is already narrowed by enabled_tools, and Jesinia's gate enforces
                # per-session tool limits and approvals server-side.
                "-c",
                'mcp_servers.jesinia.default_tools_approval_mode="approve"',
            ]
        if self.model:
            argv += ["-m", self.model]
        if req.resume_id:
            argv.append(checked_resume_id(req.resume_id))
        argv.append("-")
        return argv

    def build_env(self, req: TurnRequest) -> dict[str, str]:
        endpoint = _mcp_endpoint(req)
        return minimal_env({TOKEN_ENV: endpoint[1]} if endpoint is not None else None)

    def stdin(self, req: TurnRequest) -> str:
        if req.resume_id:
            return req.prompt
        # Codex runs with its shell disabled, so it can't open the pack files itself:
        # the first turn carries them (untrusted ones still fenced).
        pack = pack_contents(req.workdir)
        return "\n\n".join(p for p in (req.system_prompt, pack, req.prompt) if p)

    def parser(self) -> CodexStreamParser:
        return CodexStreamParser()
