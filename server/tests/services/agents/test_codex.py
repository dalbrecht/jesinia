# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import replace

from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import RunError, TextDelta, ToolResult, ToolUse, TurnDone
from jesinia.services.agents.infrastructure.codex import (
    CodexRunner,
    CodexStreamParser,
    parse_codex_line,
)
from tests.services.agents.conftest import FakeProc, read_fixture, spawn_from


def value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_codex_argv_read_only_sandbox(req: TurnRequest) -> None:
    argv = CodexRunner().build_argv(req)
    assert argv[:2] == ["codex", "exec"] and "--json" in argv
    assert value_after(argv, "--sandbox") == "read-only" and value_after(argv, "-C") == str(
        req.workdir
    )
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert 'mcp_servers.jesinia.url="http://127.0.0.1:5374/mcp"' in overrides
    assert 'mcp_servers.jesinia.bearer_token_env_var="JESINIA_MCP_TOKEN"' in overrides
    assert 'mcp_servers.jesinia.enabled_tools=["kb_read", "kb_search"]' in overrides
    assert argv[-1] == "-" and "resume" not in argv


def test_codex_resume_and_model(req: TurnRequest) -> None:
    argv = CodexRunner(model="gpt-5.6-sol").build_argv(replace(req, resume_id="t-1"))
    i = argv.index("resume")
    assert argv[i + 1] == "t-1" and argv[-1] == "-" and value_after(argv, "-m") == "gpt-5.6-sol"


def test_codex_env_and_stdin(req: TurnRequest, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    r = CodexRunner()
    env = r.build_env(req)
    assert env["JESINIA_MCP_TOKEN"] == "sess-token" and "GITHUB_TOKEN" not in env
    assert r.stdin(req).startswith(req.system_prompt) and req.prompt in r.stdin(req)
    assert r.stdin(replace(req, resume_id="t")) == req.prompt  # system prompt only on turn one


def _events(name: str) -> list[object]:
    p = CodexStreamParser()
    return [e for line in read_fixture(name) for e in p.feed(line)]


def test_parse_codex_real_turn() -> None:
    evs = _events("codex_turn.jsonl")
    assert evs == [
        TextDelta("hello from codex"),
        TurnDone("22222222-2222-4222-8222-222222222222", None),
    ]


def test_parse_codex_tool_turn() -> None:
    evs = _events("codex_tool.jsonl")
    assert evs[:2] == [ToolUse("kb_read", {"id": "p1"}), ToolResult("kb_read", True)]
    assert TextDelta("Read it.") in evs and isinstance(evs[-1], TurnDone)


def test_parse_codex_failure() -> None:
    evs = _events("codex_failed.jsonl")
    assert evs[-1] == RunError("model not supported")


def test_parse_line_ignores_noise() -> None:
    assert parse_codex_line("garbage") == [] and parse_codex_line("[1]") == []


async def test_run_uses_env_and_reports_exit(req: TurnRequest) -> None:
    proc = FakeProc(read_fixture("codex_turn.jsonl"))
    evs = [e async for e in CodexRunner(spawn=spawn_from(proc)).run(req)]
    assert isinstance(evs[-1], TurnDone) and proc.env["JESINIA_MCP_TOKEN"] == "sess-token"
    bad = FakeProc([], code=2)
    evs = [e async for e in CodexRunner(spawn=spawn_from(bad)).run(req)]
    assert evs == [RunError("codex exited with code 2")]


def test_codex_resume_id_validated(req: TurnRequest) -> None:
    import pytest

    with pytest.raises(ValueError):
        CodexRunner().build_argv(replace(req, resume_id="--last"))


def test_malformed_record_fails_an_otherwise_successful_turn() -> None:
    from jesinia.services.agents.infrastructure.codex import CodexStreamParser

    p = CodexStreamParser()
    assert p.feed('{"type": "item.completed", "item": {"type": "agent_message", "text": "Hi"') == []
    out = p.feed('{"type": "turn.completed"}')
    assert len(out) == 1 and isinstance(out[0], RunError) and "malformed record" in out[0].message


def test_first_turn_carries_the_context_pack(req: TurnRequest) -> None:
    from jesinia.services.agents.application.packs import ContextPack, UntrustedDoc, write_pack

    write_pack(
        ContextPack(
            "Discuss.",
            {"notes.md": "plain file"},
            {"subject.md": UntrustedDoc("kb:p1", "the page")},
        ),
        req.workdir,
    )
    text = CodexRunner().stdin(req)
    assert '## subject.md\n<untrusted-data source="kb:p1">\nthe page' in text
    assert "## notes.md\nplain file" in text
    assert "ROLE.md" not in text and "Bearer" not in text  # the MCP config's token stays out
    assert text.index(req.system_prompt) < text.index("# Context pack") < text.index(req.prompt)


def test_pack_inline_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jesinia.services.agents.application.packs import pack_contents

    assert pack_contents(tmp_path) == ""
    (tmp_path / "a.md").write_text("x" * 50)
    (tmp_path / "b.md").write_text("y" * 50)
    (tmp_path / ".hidden").write_text("secret")
    out = pack_contents(tmp_path, limit=60)
    assert "x" * 50 in out and "(omitted" in out and "secret" not in out
