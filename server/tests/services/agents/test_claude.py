# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from dataclasses import replace

import pytest

from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import RunError, TextDelta, ToolResult, ToolUse, TurnDone
from jesinia.services.agents.infrastructure.claude import (
    ClaudeCodeRunner,
    ClaudeStreamParser,
    parse_claude_line,
)
from tests.services.agents.conftest import FakeProc, read_fixture, spawn_from


def value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_claude_argv_confined(req: TurnRequest) -> None:
    argv = ClaudeCodeRunner().build_argv(req)
    assert argv[:2] == ["claude", "-p"] and "--strict-mcp-config" in argv
    assert {"Bash", "Edit", "Write"} <= set(value_after(argv, "--disallowedTools").split(","))
    assert value_after(argv, "--mcp-config") == str(req.mcp_config)
    assert value_after(argv, "--output-format") == "stream-json" and "--verbose" in argv
    allowed = value_after(argv, "--allowedTools").split(",")
    assert "mcp__jesinia__kb_read" in allowed and "Bash" not in allowed
    assert f"Read(//{str(req.workdir.resolve()).lstrip('/')}/**)" in allowed
    assert not {"Read", "Glob", "Grep"} & set(allowed)
    assert {"Glob", "Grep"} <= set(value_after(argv, "--disallowedTools").split(","))
    assert value_after(argv, "--append-system-prompt") == req.system_prompt
    assert req.prompt not in argv  # the prompt goes on stdin
    assert "--resume" not in argv


def test_claude_ignores_the_users_own_settings_plugins_and_skills(req: TurnRequest) -> None:
    argv = ClaudeCodeRunner().build_argv(replace(req, resume_id="abc"))
    # Built-in tools are allowlisted down to Read; MCP tools still come from --mcp-config.
    assert value_after(argv, "--tools") == "Read"
    # No user/project/local settings: no user hooks, permission rules or enabled plugins.
    assert value_after(argv, "--setting-sources") == ""
    assert "--disable-slash-commands" in argv and "--no-chrome" in argv
    # --safe-mode would also drop the --mcp-config server, leaving the agent tool-less.
    assert "--safe-mode" not in argv and "--bare" not in argv


def test_claude_resume_flag_and_model(req: TurnRequest) -> None:
    argv = ClaudeCodeRunner(model="claude-opus-5-5").build_argv(replace(req, resume_id="abc"))
    assert (
        value_after(argv, "--resume") == "abc" and value_after(argv, "--model") == "claude-opus-5-5"
    )


def _events(name: str) -> list[object]:
    p = ClaudeStreamParser()
    return [e for line in read_fixture(name) for e in p.feed(line)]


def test_parse_claude_fixture() -> None:
    evs = _events("claude_turn.jsonl")
    text = "".join(e.text for e in evs if isinstance(e, TextDelta))
    assert text == "Let me search the KB.Your Funding models note covers it."
    assert ToolUse("kb_search", {"q": "maintenance"}) in evs
    assert ToolResult("kb_search", True) in evs
    # The reply is the result's final message, not the preamble run into it.
    assert evs[-1] == TurnDone(
        "11111111-1111-4111-8111-111111111111",
        "claude-opus-5-5",
        "Your Funding models note covers it.",
    )


def test_a_preamble_tool_calls_then_json_reply_with_the_json_alone() -> None:
    answer = '{"verdict": "approve", "reasoning": "Small."}'
    lines = [
        {"type": "system", "subtype": "init", "model": "m", "session_id": "s-1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll look."}]}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "t1", "name": "mcp__jesinia__repo_git"}]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": answer},
    ]
    p = ClaudeStreamParser()
    evs = [e for line in lines for e in p.feed(json.dumps(line))]
    assert evs[-1] == TurnDone("s-1", "m", answer)


def test_a_result_without_text_falls_back_to_the_last_assistant_message() -> None:
    p = ClaudeStreamParser()
    p.feed(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}})
    )
    p.feed(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}})
    )
    done = p.feed(json.dumps({"type": "result", "subtype": "success", "session_id": "s"}))
    assert done == [TurnDone("s", None, "b")]


def test_parse_auth_error_fixture() -> None:
    evs = _events("claude_auth_error.jsonl")
    assert isinstance(evs[-1], RunError) and "Failed to authenticate" in evs[-1].message


def test_parse_line_ignores_noise() -> None:
    assert parse_claude_line("not json") == []
    assert parse_claude_line('{"type": "system", "subtype": "hook_started"}') == []
    assert parse_claude_line("[]") == []


async def test_truncated_stream_and_nonzero_exit_yield_run_error(req: TurnRequest) -> None:
    proc = FakeProc(read_fixture("truncated.jsonl"), code=1, stderr="boom: connection reset")
    evs = [e async for e in ClaudeCodeRunner(spawn=spawn_from(proc)).run(req)]
    assert isinstance(evs[-1], RunError) and "boom: connection reset" in evs[-1].message
    assert any(isinstance(e, TextDelta) for e in evs)
    assert proc.stdin == req.prompt


async def test_successful_run(req: TurnRequest) -> None:
    proc = FakeProc(read_fixture("claude_turn.jsonl"))
    evs = [e async for e in ClaudeCodeRunner(spawn=spawn_from(proc)).run(req)]
    assert isinstance(evs[-1], TurnDone)


async def test_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import jesinia.services.agents.infrastructure.cli as cli

    monkeypatch.setattr("shutil.which", lambda b: None)
    st = await ClaudeCodeRunner().status()
    assert not st.available and "not installed" in st.detail

    async def version(argv: list[str]) -> tuple[int, str]:
        return 0, "2.1.301 (Claude Code)\n"

    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_capture", version)
    st = await ClaudeCodeRunner().status()
    assert st.available and st.version == "2.1.301 (Claude Code)"

    async def broken(argv: list[str]) -> tuple[int, str]:
        return 1, ""

    monkeypatch.setattr(cli, "_capture", broken)
    assert not (await ClaudeCodeRunner().status()).available


@pytest.mark.parametrize("bad", ["--dangerously-skip-permissions", "-x", "a b", "x" * 200])
def test_resume_id_validated(req: TurnRequest, bad: str) -> None:
    with pytest.raises(ValueError):
        ClaudeCodeRunner().build_argv(replace(req, resume_id=bad))


def test_minimal_env(monkeypatch: pytest.MonkeyPatch, req: TurnRequest) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/x")
    env = ClaudeCodeRunner().build_env(req)
    assert "AWS_SECRET_ACCESS_KEY" not in env and env["CLAUDE_CONFIG_DIR"] == "/x" and "PATH" in env


def test_malformed_record_fails_an_otherwise_successful_turn() -> None:
    from jesinia.services.agents.infrastructure.claude import ClaudeStreamParser

    p = ClaudeStreamParser()
    assert (
        p.feed('{"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Hi"}}')
        == []
    )
    out = p.feed('{"type": "result", "subtype": "success", "is_error": false, "session_id": "s"}')
    assert len(out) == 1 and isinstance(out[0], RunError) and "malformed record" in out[0].message
