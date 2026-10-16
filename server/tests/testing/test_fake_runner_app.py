# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path
from typing import Any

import pytest

from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import TextDelta, ToolResult, ToolUse, TurnDone
from jesinia.testing import fake_runner_app
from jesinia.testing.fake_runner_app import DRAFT, McpScriptRunner


def _req(tmp_path: Path) -> TurnRequest:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jesinia": {
                        "url": "http://127.0.0.1:1/mcp",
                        "headers": {"Authorization": "Bearer s"},
                    }
                }
            }
        )
    )
    return TurnRequest("hi", tmp_path, "sys", None, cfg, ["kb_write"])


async def test_first_turn_calls_kb_write_then_replies(tmp_path: Path) -> None:
    sent: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        sent.append((url, headers, payload))
        return {"result": {"isError": False, "content": []}}

    runner = McpScriptRunner(post)
    assert (await runner.status()).available
    first = [e async for e in runner.run(_req(tmp_path))]
    assert first[0] == ToolUse("kb_write", DRAFT) and first[1] == ToolResult("kb_write", True)
    assert isinstance(first[-1], TurnDone) and any(isinstance(e, TextDelta) for e in first)
    url, headers, payload = sent[0]
    assert url == "http://127.0.0.1:1/mcp" and headers["Authorization"] == "Bearer s"
    assert payload["params"] == {"name": "kb_write", "arguments": DRAFT}
    second = [e async for e in runner.run(_req(tmp_path))]
    assert not any(isinstance(e, ToolUse) for e in second) and len(sent) == 1


def test_refuses_outside_a_throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JESINIA_FAKE_RUNNER", raising=False)
    with pytest.raises(RuntimeError, match="tests only"):
        fake_runner_app.build_app()
    with pytest.raises(AttributeError):
        _ = fake_runner_app.nope
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path))
    app = fake_runner_app.app
    assert app.title == "Jesinia"


async def test_real_post_against_a_live_mcp_endpoint(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import respx
    from httpx import Response

    with respx.mock:
        respx.post("http://127.0.0.1:1/mcp").mock(
            return_value=Response(200, json={"result": {"isError": True}})
        )
        runner = McpScriptRunner()
        events = [e async for e in runner.run(_req(tmp_path))]
    assert ToolResult("kb_write", False) in events


async def test_tag_prompts_get_a_json_tag_list(tmp_path: Path) -> None:
    async def post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        raise AssertionError("a tag ask never writes")

    runner = McpScriptRunner(post)
    req = TurnRequest("TAGS: which?", tmp_path, "sys", None, tmp_path / "mcp.json", [])
    events = [e async for e in runner.run(req)]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert json.loads(text) == ["fixture", "new-fixture-tag"]
    assert isinstance(events[-1], TurnDone)
