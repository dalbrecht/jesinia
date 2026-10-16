# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SessionService``: run agent turns about a subject and keep the transcript (spec §4.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from jesinia.core import auth
from jesinia.core.module import CallContext, ToolDef
from jesinia.services.agents.application.errors import (
    PendingNotFound,
    RunnerUnavailable,
    SessionBusy,
    SessionNotFound,
    TurnFailed,
)
from jesinia.services.agents.application.packs import (
    ContextPack,
    system_prompt,
    wrap_untrusted,
    write_pack,
)
from jesinia.services.agents.application.ports import SessionStore
from jesinia.services.agents.application.runner import AgentRunner, TurnRequest
from jesinia.services.agents.domain.events import RunError, TextDelta, ToolResult, ToolUse, TurnDone
from jesinia.services.agents.domain.session import (
    Origin,
    PendingWrite,
    Session,
    StoredEvent,
    Subject,
)

log = logging.getLogger(__name__)

__all__ = [
    "AttachInfo",
    "PendingNotFound",
    "RunnerUnavailable",
    "SessionBusy",
    "SessionNotFound",
    "SessionService",
    "TurnFailed",
]

ONE_SHOT_SUBJECT = "one-shot"


def _inline_context(pack: ContextPack) -> str:
    """The pack's files as prompt text: a one-shot agent has no tools to open them with.
    Untrusted documents stay fenced."""
    parts = [f"## {name}\n{text.rstrip()}" for name, text in pack.files.items()]
    parts += [f"## {name}\n{wrap_untrusted(doc)}" for name, doc in pack.untrusted.items()]
    return "\n\n".join(parts)


@dataclass(frozen=True)
class AttachInfo:
    session: Session
    pack_dir: Path
    transcript: list[StoredEvent]


class SessionService:
    def __init__(
        self,
        store: SessionStore,
        runners: Mapping[str, AgentRunner],
        sessions_dir: Path,
        mcp_url: str,
        tools: Callable[[], Mapping[str, ToolDef]],
    ) -> None:
        self.store = store
        self.runners = runners
        self.sessions_dir = sessions_dir
        self.mcp_url = mcp_url
        self._tools = tools
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._changed: dict[str, asyncio.Condition] = {}
        self._toolless: set[str] = set()  # one-shot sessions: no Jesinia tools at all

    # -- helpers -------------------------------------------------------------------
    def dir_for(self, id: str) -> Path:
        return self.sessions_dir / id

    def _cond(self, id: str) -> asyncio.Condition:
        return self._changed.setdefault(id, asyncio.Condition())

    async def _append(
        self, id: str, kind: str, data: dict[str, object], origin: Origin = "pwa"
    ) -> StoredEvent:
        event = self.store.append(id, kind, data, origin)
        cond = self._cond(id)
        async with cond:
            cond.notify_all()
        return event

    def get(self, id: str) -> Session:
        s = self.store.get_session(id)
        if s is None:
            raise SessionNotFound(id)
        return s

    def _write_mcp_config(self, id: str, token: str) -> Path:
        path = self.dir_for(id) / "mcp.json"
        cfg = {
            "mcpServers": {
                "jesinia": {
                    "type": "http",
                    "url": self.mcp_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh)
        return path

    async def recover(self) -> int:
        """After a restart, a session still marked ``running`` lost its turn: mark it
        failed (with a transcript note) so the user can send again."""
        stranded = [s for s in self.store.all_sessions() if s.status == "running"]
        for s in stranded:
            self.store.set_status(s.id, "failed")
            await self._append(
                s.id,
                "error",
                {"message": "Jesinia restarted during this turn. Send your message again."},
            )
        # A write claimed but never resolved: it may or may not have happened. Say so.
        for p in self.store.pending_in_state("approving"):
            result = {
                "error": "Jesinia restarted while applying this change; check the page "
                "before asking again."
            }
            self.store.resolve_pending(p.id, "failed", result)
            await self._append(
                p.session_id,
                "pending_resolved",
                {"pending_id": p.id, "state": "failed", "result": result},
            )
        return len(stranded)

    def restore_tokens(self) -> int:
        """Re-register session tokens after a restart (they live in each mcp.json)."""
        count = 0
        for s in self.store.all_sessions():
            path = self.dir_for(s.id) / "mcp.json"
            if path.exists():
                header = json.loads(path.read_text())["mcpServers"]["jesinia"]["headers"][
                    "Authorization"
                ]
                auth.register_session_token(str(header).removeprefix("Bearer "), s.id)
                count += 1
        return count

    # -- lifecycle -----------------------------------------------------------------
    async def create(
        self,
        subject: Subject,
        runner: str,
        pack: ContextPack,
        *,
        preauthorized_tools: frozenset[str] | set[str] = frozenset(),
        tools_allowed: bool = True,
    ) -> Session:
        if runner not in self.runners:
            raise ValueError(f"unknown runner {runner!r}")
        id = uuid.uuid4().hex
        if not tools_allowed:
            self._toolless.add(id)
        write_pack(pack, self.dir_for(id))
        token = secrets.token_urlsafe(32)
        self._write_mcp_config(id, token)
        auth.register_session_token(token, id)
        session = Session(
            id,
            subject,
            runner,
            None,
            "idle",
            datetime.now(UTC),
            None,
            frozenset(preauthorized_tools),
        )
        self.store.add_session(session)
        return session

    async def send(self, session_id: str, text: str) -> None:
        await self._start_turn(session_id, text)

    async def _start_turn(self, session_id: str, text: str) -> int:
        """Start a turn in the background; the sequence number of its ``user_message``."""
        session = self.get(session_id)
        task = self._tasks.get(session_id)
        if session.status == "running" or (task is not None and not task.done()):
            raise SessionBusy(session_id)
        self.store.set_status(session_id, "running")
        message = await self._append(session_id, "user_message", {"text": text})
        self._tasks[session_id] = asyncio.create_task(
            self._turn(session_id, text), name=f"turn:{session_id}"
        )
        return message.seq

    async def ask(self, session_id: str, prompt: str, *, timeout: float = 45.0) -> str:
        """Run one turn in an existing session (resuming the agent's own session) and
        return the text it replied with. Raises ``SessionNotFound``, ``SessionBusy``,
        ``TurnFailed`` (the agent reported an error) or ``TimeoutError``; a timed-out turn
        is stopped and marked failed so the session can be used again."""
        after = await self._start_turn(session_id, prompt)
        try:
            async with asyncio.timeout(timeout):
                return await self._reply_after(session_id, after)
        except TimeoutError:
            await self._abandon_turn(session_id, f"the turn timed out after {timeout:g}s")
            raise

    async def _reply_after(self, session_id: str, after_seq: int) -> str:
        parts: list[str] = []
        async for event in self.stream(session_id, after_seq):
            if event.kind == "text":
                parts.append(str(event.data.get("text", "")))
            elif event.kind == "error":
                raise TurnFailed(str(event.data.get("message", "the agent turn failed")))
            elif event.kind == "turn_done":
                break
        return "".join(parts)

    async def _abandon_turn(self, session_id: str, reason: str) -> None:
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.get(session_id).status == "running":
            self.store.set_status(session_id, "failed")
            await self._append(session_id, "error", {"message": reason})

    async def one_shot(
        self, runner: str, pack: ContextPack, prompt: str, *, timeout: float = 45.0
    ) -> str:
        """Ask ``runner`` one question in a throwaway session with no tools, and return
        its reply. The pack's role becomes the system prompt; its files follow the prompt
        inline (untrusted ones fenced). The session is closed whatever happens. Raises
        ``RunnerUnavailable``, ``TurnFailed`` or ``TimeoutError``."""
        if runner not in self.runners:
            raise RunnerUnavailable(runner)
        session = await self.create(
            Subject(ONE_SHOT_SUBJECT, "", "One-shot ask"),
            runner,
            ContextPack(pack.role_prompt),
            tools_allowed=False,
        )
        context = _inline_context(pack)
        text = f"{prompt}\n\n{context}" if context else prompt
        try:
            return await self.ask(session.id, text, timeout=timeout)
        finally:
            await self.close(session.id)

    async def close(self, session_id: str) -> None:
        """End a session for good: stop its turn, revoke its MCP token, and delete its
        scratch folder. The transcript stays (history)."""
        await self._abandon_turn(session_id, "the session was closed during this turn")
        path = self.dir_for(session_id) / "mcp.json"
        if path.exists():
            header = json.loads(path.read_text())["mcpServers"]["jesinia"]["headers"][
                "Authorization"
            ]
            auth.revoke_session_token(str(header).removeprefix("Bearer "))
        shutil.rmtree(self.dir_for(session_id), ignore_errors=True)
        self._toolless.discard(session_id)
        self._tasks.pop(session_id, None)
        self._changed.pop(session_id, None)

    async def _turn(self, id: str, text: str) -> None:
        session = self.get(id)
        runner = self.runners[session.runner]
        workdir = self.dir_for(id)
        req = TurnRequest(
            prompt=text,
            workdir=workdir,
            system_prompt=system_prompt(workdir),
            resume_id=session.agent_session_id,
            mcp_config=workdir / "mcp.json",
            allowed_tools=[] if id in self._toolless else sorted(self._tools()),
        )
        finished = False
        try:
            async for ev in runner.run(req):
                if isinstance(ev, TextDelta):
                    await self._append(id, "text", {"text": ev.text})
                elif isinstance(ev, ToolUse):
                    await self._append(id, "tool_use", {"name": ev.name, "input": ev.input})
                elif isinstance(ev, ToolResult):
                    await self._append(id, "tool_result", {"name": ev.name, "ok": ev.ok})
                elif isinstance(ev, TurnDone):
                    self.store.set_agent_session(id, ev.agent_session_id, ev.model)
                    self.store.set_status(id, "idle")
                    await self._append(
                        id,
                        "turn_done",
                        {"agent_session_id": ev.agent_session_id, "model": ev.model},
                    )
                    finished = True
                elif isinstance(ev, RunError):
                    self.store.set_status(id, "failed")
                    await self._append(id, "error", {"message": ev.message})
                    finished = True
            if not finished:
                self.store.set_status(id, "failed")
                await self._append(
                    id, "error", {"message": "the agent stopped without finishing the turn"}
                )
        except Exception as exc:  # never lose the transcript so far
            log.exception("agent turn failed in session %s", id)
            self.store.set_status(id, "failed")
            await self._append(id, "error", {"message": f"{type(exc).__name__}: {exc}"})

    async def wait_idle(self, session_id: str) -> None:
        task = self._tasks.get(session_id)
        if task is not None:
            await task

    async def stream(
        self, session_id: str, after_seq: int = 0, *, follow: bool = True
    ) -> AsyncIterator[StoredEvent]:
        self.get(session_id)
        seq = after_seq
        cond = self._cond(session_id)
        while True:
            batch = self.store.events(session_id, seq)
            for ev in batch:
                seq = ev.seq
                yield ev
            if not follow:
                return
            if not batch:
                async with cond:
                    await cond.wait()

    async def attach(self, session_id: str) -> AttachInfo:
        session = self.get(session_id)
        await self._append(session_id, "attached", {"by": "terminal"}, "terminal")
        return AttachInfo(session, self.dir_for(session_id), self.store.events(session_id))

    async def append_external(self, session_id: str, kind: str, data: dict[str, object]) -> None:
        self.get(session_id)
        await self._append(session_id, kind, data, "terminal")

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except BaseException:  # noqa: BLE001 - shutting down
                pass

    # -- pending writes ------------------------------------------------------------
    async def request_write(self, session_id: str, tool: ToolDef, args: BaseModel) -> PendingWrite:
        pending = PendingWrite(
            uuid.uuid4().hex, session_id, tool.name, args.model_dump(), "pending"
        )
        self.store.add_pending(pending)
        # Show the user what the write would really do (resolved by the tool), not what
        # the agent's arguments suggest.
        target: dict[str, object] = {"action": "unknown"}
        if tool.preview is not None:
            try:
                target = await tool.preview(args)
            except Exception as exc:  # noqa: BLE001 - a preview never blocks the request
                target = {"action": "unknown", "error": f"{type(exc).__name__}: {exc}"}
        await self._append(
            session_id,
            "pending_write",
            {
                "pending_id": pending.id,
                "tool": tool.name,
                "args": pending.args,
                "state": "pending",
                "target": target,
            },
        )
        return pending

    def _pending(self, session_id: str, pending_id: str) -> PendingWrite:
        p = self.store.get_pending(pending_id)
        if p is None or p.session_id != session_id:
            raise PendingNotFound(pending_id)
        return p

    async def approve(self, session_id: str, pending_id: str) -> PendingWrite:
        p = self._pending(session_id, pending_id)
        # Claim it first: a double click, a retry or a racing reject must not run it twice.
        if not self.store.claim_pending(pending_id, "approving"):
            return self._pending(session_id, pending_id)
        tool = self._tools().get(p.tool)
        try:
            if tool is None:
                raise LookupError(f"tool {p.tool!r} is no longer available")
            result = await tool.handler(
                tool.input_model.model_validate(p.args), CallContext(session_id)
            )
            state = "approved"
        except Exception as exc:
            result, state = {"error": f"{type(exc).__name__}: {exc}"}, "failed"
        self.store.resolve_pending(pending_id, state, result)  # type: ignore[arg-type]
        await self._append(
            session_id,
            "pending_resolved",
            {"pending_id": pending_id, "state": state, "result": result},
        )
        return replace(p, state=state, result=result)  # type: ignore[arg-type]

    async def reject(self, session_id: str, pending_id: str) -> PendingWrite:
        p = self._pending(session_id, pending_id)
        if not self.store.claim_pending(pending_id, "rejected"):
            return self._pending(session_id, pending_id)
        self.store.resolve_pending(pending_id, "rejected", None)
        await self._append(
            session_id, "pending_resolved", {"pending_id": pending_id, "state": "rejected"}
        )
        return replace(p, state="rejected")
