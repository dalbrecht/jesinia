# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.services.agents.domain.events import TextDelta, TurnDone
from jesinia.services.agents.infrastructure.fake import FakeRunner
from jesinia.services.agents.module import AgentsModule, default_runners
from jesinia.services.kb.module import KbModule
from tests.conftest import BASE_URL, bearer, running
from tests.mcp.test_mcp import mcp_session


@dataclass
class AppEnv:
    app: FastAPI
    cfg: Config
    client: httpx.AsyncClient
    root: Path
    agents: AgentsModule


@pytest.fixture
async def app_env(cfg: Config, tmp_path: Path) -> AsyncIterator[AppEnv]:
    root = tmp_path / "kb"
    root.mkdir()
    c = replace(cfg, kb_path=root)
    agents = AgentsModule(
        runners={"fake": FakeRunner([TextDelta("Hi there."), TurnDone("fake-1", None)])}
    )
    app = create_app(c, [KbModule(), agents])
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=bearer(c.token)
        ) as client:
            yield AppEnv(app, c, client, root, agents)


SUBJECT = {"kind": "kb", "ref": "p1", "title": "A page"}


async def _session(env: AppEnv, **extra: object) -> dict:  # type: ignore[type-arg]
    r = await env.client.post(
        "/api/agents/sessions", json={"subject": SUBJECT, "runner": "fake", **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _token(env: AppEnv, sid: str) -> str:
    cfg = json.loads((env.cfg.data_dir / "sessions" / sid / "mcp.json").read_text())
    return str(cfg["mcpServers"]["jesinia"]["headers"]["Authorization"]).removeprefix("Bearer ")


def _text(result) -> str:  # type: ignore[no-untyped-def]
    return "".join(getattr(b, "text", "") for b in result.content)


WRITE = {"title": "Agent page", "folder": "Notes", "body": "from the agent"}


async def test_session_write_goes_pending_until_approved(app_env: AppEnv) -> None:
    s = await _session(app_env)
    async with mcp_session(app_env.app, _token(app_env, s["id"])) as m:
        res = await m.call_tool("kb_write", WRITE)
    assert (
        "awaiting user approval" in _text(res)
        and not (app_env.root / "Notes/Agent page.md").exists()
    )
    pid = re.search(r"pending_id=([0-9a-f]+)", _text(res)).group(1)  # type: ignore[union-attr]
    r = await app_env.client.post(f"/api/agents/sessions/{s['id']}/pending/{pid}/approve")
    assert r.status_code == 200 and r.json()["state"] == "approved"
    assert (app_env.root / "Notes/Agent page.md").exists()


async def test_rejected_write_never_happens(app_env: AppEnv) -> None:
    s = await _session(app_env)
    async with mcp_session(app_env.app, _token(app_env, s["id"])) as m:
        res = await m.call_tool("kb_write", WRITE)
    pid = re.search(r"pending_id=([0-9a-f]+)", _text(res)).group(1)  # type: ignore[union-attr]
    r = await app_env.client.post(f"/api/agents/sessions/{s['id']}/pending/{pid}/reject")
    assert r.json()["state"] == "rejected" and not (app_env.root / "Notes").exists()
    assert (
        await app_env.client.post(f"/api/agents/sessions/{s['id']}/pending/nope/approve")
    ).status_code == 404


async def test_preauthorized_write_runs_directly(app_env: AppEnv) -> None:
    s = await _session(app_env, preauthorized_tools=["kb_write"])
    async with mcp_session(app_env.app, _token(app_env, s["id"])) as m:
        res = await m.call_tool("kb_write", WRITE)
    assert not res.is_error and json.loads(_text(res))["path"] == "Notes/Agent page.md"


async def test_turn_and_sse_resume_after_seq(app_env: AppEnv) -> None:
    s = await _session(app_env)
    assert (
        await app_env.client.post(f"/api/agents/sessions/{s['id']}/messages", json={"text": "hi"})
    ).status_code == 202
    await app_env.agents.service.wait_idle(s["id"])
    r = await app_env.client.get(
        f"/api/agents/sessions/{s['id']}/events", params={"follow": "false"}
    )
    assert r.headers["content-type"].startswith("text/event-stream")
    ids = [int(x) for x in re.findall(r"^id: (\d+)$", r.text, re.M)]
    assert ids == [1, 2, 3] and "event: turn_done" in r.text
    later = await app_env.client.get(
        f"/api/agents/sessions/{s['id']}/events",
        params={"follow": "false"},
        headers={"Last-Event-ID": "2"},
    )
    assert re.findall(r"^id: (\d+)$", later.text, re.M) == ["3"]
    after = await app_env.client.get(
        f"/api/agents/sessions/{s['id']}/events", params={"follow": "false", "after": 1}
    )
    assert re.findall(r"^id: (\d+)$", after.text, re.M) == ["2", "3"]
    got = (await app_env.client.get(f"/api/agents/sessions/{s['id']}")).json()
    assert got["agent_session_id"] == "fake-1" and got["status"] == "idle"


async def test_attach_returns_pack_and_transcript_and_terminal_notes_stream(
    app_env: AppEnv,
) -> None:
    s = await _session(app_env)
    async with mcp_session(app_env.app, app_env.cfg.token) as m:
        info = json.loads(_text(await m.call_tool("agents_session_attach", {"id": s["id"]})))
        await m.call_tool(
            "agents_session_note", {"id": s["id"], "text": "continuing in the terminal"}
        )
    assert info["pack_dir"].endswith(s["id"]) and info["session"]["subject"]["title"] == "A page"
    r = await app_env.client.get(
        f"/api/agents/sessions/{s['id']}/events", params={"follow": "false"}
    )
    assert '"origin": "terminal"' in r.text and "continuing in the terminal" in r.text


async def test_session_cannot_attach_to_another(app_env: AppEnv) -> None:
    a, b = await _session(app_env), await _session(app_env)
    async with mcp_session(app_env.app, _token(app_env, a["id"])) as m:
        res = await m.call_tool("agents_session_attach", {"id": b["id"]})
        assert res.is_error and "PermissionError" in _text(res)
        ok = await m.call_tool("agents_session_note", {"id": a["id"], "text": "self is fine"})
        assert not ok.is_error


async def test_errors_and_runners(app_env: AppEnv) -> None:
    c = app_env.client
    assert (await c.get("/api/agents/sessions/nope")).status_code == 404
    assert (
        await c.post("/api/agents/sessions/nope/messages", json={"text": "x"})
    ).status_code == 404
    assert (await c.get("/api/agents/sessions/nope/events")).status_code == 404
    bad = await c.post("/api/agents/sessions", json={"subject": SUBJECT, "runner": "nope"})
    assert bad.status_code == 400
    assert (await c.get("/api/agents/runners")).json()["fake"]["available"] is True
    assert (await c.get("/healthz")).json()["modules"]["agents"] == {
        "ok": True,
        "detail": "fake ok",
    }


async def test_busy_is_409(cfg: Config, tmp_path: Path) -> None:
    agents = AgentsModule(
        runners={"fake": FakeRunner([TextDelta("s"), TurnDone("f", None)], delay_ms=300)}
    )
    app = create_app(replace(cfg, kb_path=None), [agents])
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=bearer(cfg.token)
        ) as c:
            s = (
                await c.post("/api/agents/sessions", json={"subject": SUBJECT, "runner": "fake"})
            ).json()
            await c.post(f"/api/agents/sessions/{s['id']}/messages", json={"text": "1"})
            assert (
                await c.post(f"/api/agents/sessions/{s['id']}/messages", json={"text": "2"})
            ).status_code == 409


def test_default_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JESINIA_FAKE_RUNNER", raising=False)
    assert set(default_runners()) == {"claude-code", "codex"}
    monkeypatch.setenv("JESINIA_FAKE_RUNNER", "1")
    monkeypatch.delenv("JESINIA_HOME", raising=False)
    assert "fake" not in default_runners()  # never in a real install
    monkeypatch.setenv("JESINIA_HOME", "/tmp/x")
    assert "fake" in default_runners()


def test_module_before_start() -> None:
    from fastapi import HTTPException

    mod = AgentsModule()
    assert mod.health().ok is False
    with pytest.raises(HTTPException):
        _ = mod.service


async def test_pending_write_events_show_the_real_target(app_env: AppEnv) -> None:
    from pathlib import PurePosixPath

    from jesinia.services.kb.application.service import PageDraft

    kb = app_env.app.state.ctx.services["kb"]
    existing = await kb.write(PageDraft(None, "Existing", PurePosixPath("Ideas"), "old body"))
    s = await _session(app_env)
    async with mcp_session(app_env.app, _token(app_env, s["id"])) as m:
        await m.call_tool("kb_write", WRITE)
        # An "update" that names a new title and folder still targets the existing page.
        await m.call_tool(
            "kb_write",
            {
                "id": existing.page.id,
                "title": "Harmless",
                "folder": "Notes",
                "body": "x",
                "expected_rev": existing.rev,
            },
        )
        await m.call_tool("kb_move", {"id": existing.page.id, "folder": "Archive"})
        await m.call_tool("kb_move", {"id": "0" * 36, "folder": "Archive"})
        await m.call_tool(
            "kb_write",
            {"id": existing.page.id, "title": "Existing", "folder": "Ideas", "body": "x",
             "jesinia": {"kind": "note"}, "expected_rev": existing.rev},
        )  # fmt: skip
    r = await app_env.client.get(
        f"/api/agents/sessions/{s['id']}/events", params={"follow": "false"}
    )
    targets = [
        json.loads(line.removeprefix("data: "))["data"]["target"]
        for line in r.text.splitlines()
        if line.startswith("data: ") and '"pending_write"' in line
    ]
    assert targets[0] == {"action": "create", "path": "Notes/Agent page.md", "title": "Agent page"}
    assert targets[1] == {
        "action": "update+move",
        "path": "Ideas/Existing.md",
        "title": "Existing",
        "current": "old body",
        "to": "Notes/Harmless.md",
        "new_title": "Harmless",
    }
    assert targets[2] == {
        "action": "move",
        "path": "Ideas/Existing.md",
        "to": "Archive/Existing.md",
    }
    assert targets[3]["action"] == "unknown" and "PageNotFound" in targets[3]["error"]
    assert targets[4] == {
        "action": "update",
        "path": "Ideas/Existing.md",
        "title": "Existing",
        "current": "old body",
        "jesinia": {"from": {}, "to": {"kind": "note"}},
    }


async def test_previews_use_the_real_target_resolution(app_env: AppEnv) -> None:
    from pathlib import PurePosixPath

    from jesinia.services.kb.application.service import PageDraft

    kb = app_env.app.state.ctx.services["kb"]
    await kb.write(PageDraft(None, "Taken", PurePosixPath("Notes"), "x"))
    s = await _session(app_env)
    async with mcp_session(app_env.app, _token(app_env, s["id"])) as m:
        await m.call_tool("kb_write", {"title": "A/B: c", "folder": "Notes", "body": "x"})
        await m.call_tool("kb_write", {"title": "Taken", "folder": "Notes", "body": "y"})
    r = await app_env.client.get(
        f"/api/agents/sessions/{s['id']}/events", params={"follow": "false"}
    )
    targets = [
        json.loads(line.removeprefix("data: "))["data"]["target"]
        for line in r.text.splitlines()
        if line.startswith("data: ") and '"pending_write"' in line
    ]
    assert targets[0] == {"action": "create", "path": "Notes/A B c.md", "title": "A/B: c"}
    # The id is only chosen at write time, so the preview names it instead of guessing.
    assert targets[1]["path"] == "Notes/Taken [new page id].md"
    assert "id will be added" in targets[1]["note"]


def test_preferred_runner_is_the_first_available_preferring_claude_code() -> None:
    mod = AgentsModule()
    assert mod.preferred_runner() is None  # not started: nothing known to be available
    mod._status = {
        "codex": {"available": True},
        "claude-code": {"available": True},
    }
    assert mod.preferred_runner() == "claude-code"
    mod._status["claude-code"] = {"available": False}
    assert mod.preferred_runner() == "codex"
    mod._status["codex"] = {"available": False}
    assert mod.preferred_runner() is None
