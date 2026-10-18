# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from jesinia.apps.issues.application.agent_output import parse_agent_json
from jesinia.apps.issues.application.agent_output import schema_for as issues_schema_for
from jesinia.apps.review.application.jobs import parse_job_output, schema_for
from jesinia.apps.review.domain.jobs import JobKind
from jesinia.apps.review.module import ReviewModule
from jesinia.core.config import Config
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Scheduler
from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import TextDelta, ToolResult, ToolUse, TurnDone
from jesinia.services.fetch.application.service import FetchBlocked, FetchService
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


def test_loopback_fetch_needs_the_throwaway_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JESINIA_FAKE_RUNNER", raising=False)
    monkeypatch.setenv("JESINIA_HOME", "/tmp/anything")
    with pytest.raises(RuntimeError, match="tests only"):
        fake_runner_app.fixture_fetch_module()
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.delenv("JESINIA_HOME")
    with pytest.raises(RuntimeError, match="tests only"):
        fake_runner_app.fixture_fetch_module()


def test_loopback_fetch_refuses_a_home_outside_the_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    outside = Path.home() / ".jesinia"
    sneaky = Path(tempfile.gettempdir()) / ".." / "jesinia-home"
    for home in (outside, sneaky, Path(tempfile.gettempdir())):
        monkeypatch.setenv("JESINIA_HOME", str(home))
        with pytest.raises(RuntimeError, match="temporary directory"):
            fake_runner_app.fixture_fetch_module()
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "data"))  # need not exist yet
    assert fake_runner_app.fixture_fetch_module().name == "fetch"


async def test_loopback_fetch_allows_only_loopback_beyond_public(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cfg: Config
) -> None:
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path))
    module = fake_runner_app.fixture_fetch_module()
    ctx = AppContext(cfg, EventBus(), Scheduler())
    await module.start(ctx)
    try:
        fetch = ctx.services["fetch"]
        assert isinstance(fetch, FetchService)
        assert await fetch.check("http://127.0.0.1:5999/feed.xml") == "127.0.0.1"
        assert await fetch.check("http://[::1]:5999/feed.xml") == "::1"
        for blocked in ("http://10.0.0.5/", "http://169.254.169.254/", "http://localhost/"):
            with pytest.raises(FetchBlocked):
                await fetch.check(blocked)
    finally:
        await module.stop()
    assert fake_runner_app.allow_loopback("93.184.216.34")
    assert not fake_runner_app.allow_loopback("192.168.1.1")
    assert not fake_runner_app.allow_loopback("not an address")


async def test_the_e2e_app_never_reads_a_real_github_token() -> None:
    # The e2e server must never sync the developer's own GitHub account: it runs signed out
    # of GitHub, so the review app shows its auth-missing state deterministically.
    module = fake_runner_app.signed_out_github_module()
    assert module.name == "github"
    assert await fake_runner_app.no_github_token() is None


def test_build_app_wires_the_signed_out_github_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jesinia.core.app as core_app
    from jesinia.services.github.module import GitHubModule

    captured: list[object] = []

    def capture(cfg: Config, mods: list[object], **_: object) -> object:
        captured.extend(mods)
        return object()

    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path))
    monkeypatch.delenv("JESINIA_FAKE_GITHUB_URL", raising=False)
    monkeypatch.setattr(core_app, "create_app", capture)
    fake_runner_app.build_app()
    github = [m for m in captured if getattr(m, "name", None) == "github"]
    assert len(github) == 1 and isinstance(github[0], GitHubModule)
    assert github[0]._token is fake_runner_app.no_github_token


def test_the_fake_github_is_reachable_only_from_a_throwaway_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JESINIA_FAKE_GITHUB_URL", "http://127.0.0.1:5999")
    monkeypatch.delenv("JESINIA_FAKE_RUNNER", raising=False)
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "data"))
    with pytest.raises(RuntimeError, match="tests only"):
        fake_runner_app.github_module()
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(Path.home() / ".jesinia"))
    with pytest.raises(RuntimeError, match="temporary directory"):
        fake_runner_app.github_module()
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "data"))
    module = fake_runner_app.github_module()
    assert module._token is fake_runner_app.fake_github_token
    assert isinstance(module._transport, fake_runner_app.LoopbackGitHubTransport)


def test_the_fake_github_must_be_loopback_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "data"))
    for url in (
        "https://api.github.com",
        "http://10.0.0.5:80",
        "http://127.0.0.1",
        "ftp://127.0.0.1:1",
    ):
        monkeypatch.setenv("JESINIA_FAKE_GITHUB_URL", url)
        with pytest.raises(RuntimeError, match="loopback"):
            fake_runner_app.github_module()


async def test_the_fake_github_token_is_a_fixed_test_value() -> None:
    assert await fake_runner_app.fake_github_token() == "e2e-token"


async def test_the_loopback_transport_sends_every_request_to_the_fake() -> None:
    import httpx

    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = fake_runner_app.LoopbackGitHubTransport(
        "http://127.0.0.1:5999", httpx.MockTransport(respond)
    )
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=transport) as client:
        assert (await client.get("/user", params={"a": "b"})).json() == {"ok": True}
    (request,) = seen
    assert str(request.url) == "http://127.0.0.1:5999/user?a=b"
    assert request.headers["host"] == "127.0.0.1:5999"
    await transport.aclose()


def test_the_review_workspace_fetches_from_fixture_repositories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("JESINIA_FAKE_GITHUB_REPOS", raising=False)
    plain = fake_runner_app.review_module()
    assert plain._remote_url is None
    monkeypatch.setenv("JESINIA_FAKE_GITHUB_REPOS", str(tmp_path / "repos"))
    module = fake_runner_app.review_module()
    assert module._remote_url is not None
    assert module._remote_url("e2e-dev/greeter") == str(tmp_path / "repos" / "e2e-dev/greeter.git")


def test_build_app_points_github_and_review_at_the_fixtures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jesinia.core.app as core_app
    from jesinia.services.github.module import GitHubModule

    captured: list[object] = []

    def capture(cfg: Config, mods: list[object], **_: object) -> object:
        captured.extend(mods)
        return object()

    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("JESINIA_FAKE_GITHUB_URL", "http://127.0.0.1:5999")
    monkeypatch.setenv("JESINIA_FAKE_GITHUB_REPOS", str(tmp_path / "repos"))
    monkeypatch.setattr(core_app, "create_app", capture)
    fake_runner_app.build_app()
    github = next(m for m in captured if isinstance(m, GitHubModule))
    review = next(m for m in captured if isinstance(m, ReviewModule))
    assert github._token is fake_runner_app.fake_github_token
    assert review._remote_url is not None
    assert review._remote_url("e2e-dev/greeter").endswith("e2e-dev/greeter.git")


REVIEW_JOBS = [kind for kind in JobKind if kind is not JobKind.STACK_WALKTHROUGH]


@pytest.mark.parametrize("job", REVIEW_JOBS, ids=lambda kind: kind.value)
def test_every_review_job_gets_an_output_valid_against_its_real_schema(job: JobKind) -> None:
    prompt = f"Run the {job.value} job. Depth: deep.\n\nReply with only one JSON object"
    reply = fake_runner_app.review_job_reply(prompt)
    assert reply is not None
    assert parse_job_output(reply, schema_for(job))


def test_a_prompt_that_is_not_a_review_job_gets_no_review_output() -> None:
    assert fake_runner_app.review_job_reply("What is this page about?") is None
    assert fake_runner_app.review_job_reply("Run the stack_walkthrough job. Depth: deep.") is None
    assert fake_runner_app.review_job_reply("Run the nonsense job.") is None


async def test_a_review_job_turn_replies_with_json_and_never_writes(tmp_path: Path) -> None:
    async def post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        raise AssertionError("a review job never writes")

    runner = McpScriptRunner(post)
    req = TurnRequest(
        "Run the findings job. Depth: quick.", tmp_path, "sys", None, tmp_path / "m", []
    )
    events = [e async for e in runner.run(req)]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert parse_job_output(text, schema_for(JobKind.FINDINGS))["findings"]
    assert isinstance(events[-1], TurnDone)


async def test_a_triage_ask_gets_a_reply_valid_against_the_issues_schema(tmp_path: Path) -> None:
    """The Issues e2e approves the fake runner's suggestion (PWA Task 12)."""

    async def post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        raise AssertionError("a triage ask never writes")

    runner = McpScriptRunner(post)
    prompt = "Triage the issue. Reply with only one JSON object, and nothing else."
    req = TurnRequest(prompt, tmp_path, "sys", None, tmp_path / "m", [])
    events = [e async for e in runner.run(req)]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    reply = parse_agent_json(text, issues_schema_for("triage"))
    assert reply["labels"] == [
        {
            "dimension": "readiness",
            "value": "agent-ready",
            "reason": "The steps to reproduce are clear.",
        }
    ]
    assert isinstance(events[-1], TurnDone)
