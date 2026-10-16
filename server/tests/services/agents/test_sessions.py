# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import BaseModel

from jesinia.core import auth
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import CallContext, ToolDef
from jesinia.services.agents.application.packs import ContextPack, UntrustedDoc
from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.application.sessions import (
    PendingNotFound,
    SessionBusy,
    SessionNotFound,
    SessionService,
)
from jesinia.services.agents.domain.events import (
    AgentEvent,
    RunError,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnDone,
)
from jesinia.services.agents.domain.session import StoredEvent, Subject
from jesinia.services.agents.infrastructure.fake import FakeRunner
from jesinia.services.agents.infrastructure.store import SqliteSessionStore

DB = ModuleDb("sessions", DbClass.HISTORY, files("jesinia.services.agents.migrations"))
SUBJ = Subject("kb", "p1", "A page")


class In(BaseModel):
    title: str = "t"


async def _write(args: BaseModel, call: CallContext) -> object:
    return {"wrote": args.model_dump(), "session": call.session_id}


async def _boom(args: BaseModel, call: CallContext) -> object:
    raise RuntimeError("disk full")


TOOLS = {
    "kb_write": ToolDef("kb_write", "w", In, _write, True),
    "kb_bad": ToolDef("kb_bad", "w", In, _boom, True),
    "kb_read": ToolDef("kb_read", "r", In, _write, False),
}


def pack() -> ContextPack:
    return ContextPack("Discuss.", {}, {"subject.md": UntrustedDoc("kb:p1", "body")})


_OPEN: list[object] = []


@pytest.fixture(autouse=True)
def _close_connections():  # type: ignore[no-untyped-def]
    yield
    while _OPEN:
        _OPEN.pop().close()  # type: ignore[attr-defined]


def make(tmp_path: Path, runner: object) -> SessionService:
    conn = open_db(tmp_path / "data", DB)
    _OPEN.append(conn)
    return SessionService(
        SqliteSessionStore(conn),
        {"fake": runner},  # type: ignore[dict-item]
        tmp_path / "sessions",
        "http://127.0.0.1:4870/mcp",
        lambda: TOOLS,
    )


async def collect(svc: SessionService, sid: str, until: str) -> list[StoredEvent]:
    out: list[StoredEvent] = []

    async def run() -> None:
        async for e in svc.stream(sid):
            out.append(e)
            if e.kind == until:
                return

    await asyncio.wait_for(run(), timeout=5)
    return out


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner(
        [
            TextDelta("Hel"),
            TextDelta("lo"),
            ToolUse("kb_read", {"id": "p1"}),
            ToolResult("kb_read", True),
            TurnDone("fake-1", "fake-model"),
        ]
    )


@pytest.fixture
def svc(tmp_path: Path, fake: FakeRunner) -> SessionService:
    return make(tmp_path, fake)


async def test_turn_events_persist_and_stream(svc: SessionService) -> None:
    s = await svc.create(SUBJ, "fake", pack())
    await svc.send(s.id, "hi")
    evs = await collect(svc, s.id, until="turn_done")
    assert [e.kind for e in evs] == [
        "user_message",
        "text",
        "text",
        "tool_use",
        "tool_result",
        "turn_done",
    ]
    got = svc.get(s.id)
    assert got.agent_session_id == "fake-1" and got.model == "fake-model" and got.status == "idle"


async def test_session_dir_pack_and_token(svc: SessionService) -> None:
    s = await svc.create(SUBJ, "fake", pack())
    d = svc.dir_for(s.id)
    assert (d / "subject.md").read_text().startswith('<untrusted-data source="kb:p1">')
    cfg = json.loads((d / "mcp.json").read_text())["mcpServers"]["jesinia"]
    assert (
        cfg["url"] == "http://127.0.0.1:4870/mcp"
        and oct((d / "mcp.json").stat().st_mode)[-3:] == "600"
    )
    token = cfg["headers"]["Authorization"].removeprefix("Bearer ")
    assert auth.session_for_token(token) == s.id
    auth.reset_state()
    assert svc.restore_tokens() == 1 and auth.session_for_token(token) == s.id


async def test_second_turn_resumes_agent_session(svc: SessionService, fake: FakeRunner) -> None:
    s = await svc.create(SUBJ, "fake", pack())
    await svc.send(s.id, "one")
    await svc.wait_idle(s.id)
    await svc.send(s.id, "two")
    await svc.wait_idle(s.id)
    first, second = fake.requests
    assert first.resume_id is None and second.resume_id == "fake-1"
    assert "Discuss." in first.system_prompt and sorted(first.allowed_tools) == sorted(TOOLS)


class Failing:
    name = "fake"

    async def status(self) -> RunnerStatus:
        return RunnerStatus(True, None, "")

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        yield TextDelta("partial answer")
        yield RunError("the CLI is logged out")


async def test_failed_run_keeps_transcript(tmp_path: Path) -> None:
    svc = make(tmp_path, Failing())
    s = await svc.create(SUBJ, "fake", pack())
    await svc.send(s.id, "hi")
    await svc.wait_idle(s.id)
    kinds = [e.kind for e in svc.store.events(s.id)]
    assert kinds == ["user_message", "text", "error"] and svc.get(s.id).status == "failed"
    await svc.send(s.id, "retry")  # failed sessions can be retried


class Silent:
    name = "fake"

    def __init__(self, events: Sequence[AgentEvent] = (), crash: bool = False) -> None:
        self.events, self.crash = list(events), crash

    async def status(self) -> RunnerStatus:
        return RunnerStatus(True, None, "")

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        for e in self.events:
            yield e
        if self.crash:
            raise OSError("pipe broke")


async def test_stream_without_final_event_or_crash_fails(tmp_path: Path) -> None:
    svc = make(tmp_path, Silent([TextDelta("x")]))
    s = await svc.create(SUBJ, "fake", pack())
    await svc.send(s.id, "hi")
    await svc.wait_idle(s.id)
    assert (
        svc.store.events(s.id)[-1].data["message"] == "the agent stopped without finishing the turn"
    )
    svc2 = make(tmp_path / "b", Silent(crash=True))
    s2 = await svc2.create(SUBJ, "fake", pack())
    await svc2.send(s2.id, "hi")
    await svc2.wait_idle(s2.id)
    assert "OSError: pipe broke" in str(svc2.store.events(s2.id)[-1].data["message"])


async def test_send_while_running_raises_busy(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRunner([TextDelta("slow"), TurnDone("f", None)], delay_ms=200))
    s = await svc.create(SUBJ, "fake", pack())
    await svc.send(s.id, "one")
    with pytest.raises(SessionBusy):
        await svc.send(s.id, "two")
    await svc.stop()


async def test_unknown_session_and_runner(svc: SessionService) -> None:
    with pytest.raises(SessionNotFound):
        svc.get("nope")
    with pytest.raises(ValueError):
        await svc.create(SUBJ, "nope", pack())
    with pytest.raises(SessionNotFound):
        await svc.append_external("nope", "text", {})


async def test_pending_write_lifecycle(svc: SessionService) -> None:
    s = await svc.create(SUBJ, "fake", pack())
    p = await svc.request_write(s.id, TOOLS["kb_write"], In(title="X"))
    approved = await svc.approve(s.id, p.id)
    assert approved.state == "approved" and approved.result == {
        "wrote": {"title": "X"},
        "session": s.id,
    }
    assert (await svc.approve(s.id, p.id)).state == "approved"  # idempotent
    p2 = await svc.request_write(s.id, TOOLS["kb_write"], In())
    assert (await svc.reject(s.id, p2.id)).state == "rejected"
    assert (await svc.reject(s.id, p2.id)).state == "rejected"
    p3 = await svc.request_write(s.id, TOOLS["kb_bad"], In())
    failed = await svc.approve(s.id, p3.id)
    assert failed.state == "failed" and "disk full" in str(failed.result)
    with pytest.raises(PendingNotFound):
        await svc.approve("other-session", p.id)
    kinds = [e.kind for e in svc.store.events(s.id)]
    assert kinds.count("pending_write") == 3 and kinds.count("pending_resolved") == 3


async def test_approve_when_tool_disappeared(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRunner([]))
    s = await svc.create(SUBJ, "fake", pack())
    p = await svc.request_write(s.id, ToolDef("gone_write", "", In, _write, True), In())
    assert (await svc.approve(s.id, p.id)).state == "failed"


async def test_attach_and_external_notes(svc: SessionService) -> None:
    s = await svc.create(SUBJ, "fake", pack())
    await svc.send(s.id, "hi")
    await svc.wait_idle(s.id)
    info = await svc.attach(s.id)
    assert info.pack_dir == svc.dir_for(s.id) and info.transcript[0].kind == "user_message"
    await svc.append_external(s.id, "text", {"text": "from terminal"})
    last = svc.store.events(s.id)[-1]
    assert last.origin == "terminal" and last.data == {"text": "from terminal"}


async def test_stream_follow_wakes_on_new_events(svc: SessionService) -> None:
    s = await svc.create(SUBJ, "fake", pack())
    seen: list[str] = []

    async def reader() -> None:
        async for e in svc.stream(s.id):
            seen.append(e.kind)
            if e.kind == "attached":
                return

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)
    await svc.attach(s.id)
    await asyncio.wait_for(task, timeout=2)
    assert seen == ["attached"]
    assert [e.kind async for e in svc.stream(s.id, 0, follow=False)] == ["attached"]


async def test_racing_approvals_run_the_write_once(tmp_path: Path) -> None:
    runs: list[str] = []
    gate = asyncio.Event()

    async def slow(args: BaseModel, call: CallContext) -> object:
        runs.append("ran")
        await gate.wait()
        return {"ok": True}

    tools = {**TOOLS, "kb_slow": ToolDef("kb_slow", "w", In, slow, True)}
    svc = make(tmp_path, FakeRunner([]))
    svc._tools = lambda: tools
    s = await svc.create(SUBJ, "fake", pack())
    p = await svc.request_write(s.id, tools["kb_slow"], In())
    first = asyncio.create_task(svc.approve(s.id, p.id))
    await asyncio.sleep(0)
    second = await svc.approve(s.id, p.id)  # while the first is still running
    rejected = await svc.reject(s.id, p.id)
    assert second.state == "approving" and rejected.state == "approving"
    gate.set()
    assert (await first).state == "approved" and runs == ["ran"]
    assert [e.kind for e in svc.store.events(s.id)].count("pending_resolved") == 1


async def test_restart_recovers_a_session_stranded_mid_turn(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRunner([]))
    s = await svc.create(SUBJ, "fake", pack())
    svc.store.set_status(s.id, "running")  # the server died during this turn
    again = make(tmp_path, FakeRunner([TextDelta("ok"), TurnDone("f", None)]))
    assert await again.recover() == 1
    assert again.get(s.id).status == "failed"
    assert str(again.store.events(s.id)[-1].data["message"]).startswith("Jesinia restarted")
    await again.send(s.id, "hello again")  # no longer stuck as busy
    await again.wait_idle(s.id)
    assert again.get(s.id).status == "idle"


async def test_restart_fails_writes_stranded_mid_approval(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRunner([]))
    s = await svc.create(SUBJ, "fake", pack())
    p = await svc.request_write(s.id, TOOLS["kb_write"], In())
    assert svc.store.claim_pending(p.id, "approving")  # then the server died
    again = make(tmp_path, FakeRunner([]))
    await again.recover()
    stuck = again.store.get_pending(p.id)
    assert stuck is not None and stuck.state == "failed" and "restarted" in str(stuck.result)
    assert again.store.events(s.id)[-1].kind == "pending_resolved"
    assert (await again.approve(s.id, p.id)).state == "failed"
