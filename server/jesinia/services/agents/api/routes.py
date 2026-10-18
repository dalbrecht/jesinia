# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/api/agents/…`` routes (incl. the SSE transcript) and the terminal-attach MCP tools."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from jesinia.core.idempotency import IdempotencyStore, IdempotentCall, idempotent
from jesinia.core.module import CallContext, ToolDef
from jesinia.services.agents.application.packs import ContextPack, UntrustedDoc
from jesinia.services.agents.application.sessions import (
    PendingNotFound,
    SessionBusy,
    SessionNotFound,
    SessionService,
)
from jesinia.services.agents.application.settings import RUNNER_MODEL_ENV, RunnerModels
from jesinia.services.agents.domain.session import PendingWrite, Session, StoredEvent, Subject
from jesinia.services.agents.domain.settings import (
    MODEL_NAME_MAX,
    MODEL_NAME_PATTERN,
    EffectiveModel,
    checked_model_name,
)


class AgentsHost(Protocol):
    @property
    def service(self) -> SessionService: ...

    @property
    def models(self) -> RunnerModels: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...

    async def runner_status(self) -> dict[str, dict[str, object]]: ...


class SubjectIn(BaseModel):
    kind: str
    ref: str
    title: str


class UntrustedIn(BaseModel):
    source: str
    text: str


class SessionIn(BaseModel):
    subject: SubjectIn
    runner: str
    role_prompt: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    untrusted: dict[str, UntrustedIn] = Field(default_factory=dict)
    preauthorized_tools: list[str] = Field(default_factory=list)


class MessageIn(BaseModel):
    text: str = Field(min_length=1)


class IdIn(BaseModel):
    id: str


class NoteIn(BaseModel):
    id: str
    text: str = Field(min_length=1)


# Validated at the boundary: the name becomes one argv value (``-m`` / ``--model``).
ModelName = Annotated[
    str,
    Field(min_length=1, max_length=MODEL_NAME_MAX, pattern=MODEL_NAME_PATTERN),
    AfterValidator(checked_model_name),
]


class RunnerModelOut(BaseModel):
    setting: str | None
    environment: str | None
    variable: str  # the environment variable the runner falls back to
    effective: str | None  # None: the CLI's own default
    source: Literal["setting", "environment", "default"]

    @classmethod
    def of(cls, runner: str, m: EffectiveModel) -> RunnerModelOut:
        return cls(
            setting=m.setting,
            environment=m.environment,
            variable=RUNNER_MODEL_ENV[runner],
            effective=m.model,
            source=m.source,
        )


class AgentSettingsOut(BaseModel):
    """Settings → Agents: each runner's model and where it comes from."""

    models: dict[str, RunnerModelOut]

    @classmethod
    def of(cls, models: dict[str, EffectiveModel]) -> AgentSettingsOut:
        return cls(models={runner: RunnerModelOut.of(runner, m) for runner, m in models.items()})


class AgentSettingsPatch(BaseModel):
    """A partial update: a runner left out keeps its model; ``null`` clears it (back to the
    environment variable, else the CLI's default)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    codex: ModelName | None = None
    claude_code: ModelName | None = Field(default=None, alias="claude-code")


def session_json(s: Session) -> dict[str, object]:
    return {
        "id": s.id,
        "subject": {"kind": s.subject.kind, "ref": s.subject.ref, "title": s.subject.title},
        "runner": s.runner,
        "agent_session_id": s.agent_session_id,
        "model": s.model,
        "status": s.status,
        "created_at": s.created_at.isoformat(),
        "preauthorized": sorted(s.preauthorized),
        "read_only": s.read_only,
    }


def event_json(e: StoredEvent) -> dict[str, object]:
    return {"seq": e.seq, "kind": e.kind, "data": e.data, "origin": e.origin}


def pending_json(p: PendingWrite) -> dict[str, object]:
    return {"id": p.id, "tool": p.tool, "args": p.args, "state": p.state, "result": p.result}


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"not found: {exc}")


def build_router(host: AgentsHost) -> APIRouter:
    r = APIRouter(prefix="/api/agents", tags=["agents"])
    keyed = Depends(idempotent(lambda: host.idempotency))

    @r.get("/settings", response_model=AgentSettingsOut)
    async def get_settings() -> AgentSettingsOut:
        return AgentSettingsOut.of(host.models.all())

    @r.patch("/settings", response_model=AgentSettingsOut)
    async def patch_settings(body: AgentSettingsPatch, idem: IdempotentCall = keyed) -> Response:
        """Save Settings → Agents. Applies from each runner's next turn, no restart needed."""

        async def run() -> Response:
            sent = body.model_fields_set
            changes: dict[str, str | None] = {}
            if "codex" in sent:
                changes["codex"] = body.codex
            if "claude_code" in sent:
                changes["claude-code"] = body.claude_code
            saved = AgentSettingsOut.of(host.models.update(changes))
            return JSONResponse(saved.model_dump())

        return await idem.run(run)

    @r.get("/runners")
    async def runners() -> dict[str, dict[str, object]]:
        return await host.runner_status()

    @r.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create(body: SessionIn) -> dict[str, object]:
        pack = ContextPack(
            body.role_prompt,
            body.files,
            {k: UntrustedDoc(v.source, v.text) for k, v in body.untrusted.items()},
        )
        try:
            s = await host.service.create(
                Subject(body.subject.kind, body.subject.ref, body.subject.title),
                body.runner,
                pack,
                preauthorized_tools=set(body.preauthorized_tools),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return session_json(s)

    @r.get("/sessions/{id}")
    async def get(id: str) -> dict[str, object]:
        try:
            return session_json(host.service.get(id))
        except SessionNotFound as exc:
            raise _not_found(exc) from exc

    @r.post("/sessions/{id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def send(id: str, body: MessageIn) -> dict[str, object]:
        try:
            await host.service.send(id, body.text)
        except SessionNotFound as exc:
            raise _not_found(exc) from exc
        except SessionBusy as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "a turn is already running") from exc
        return {"ok": True}

    @r.get("/sessions/{id}/events")
    async def events(
        id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        follow: bool = True,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        try:
            host.service.get(id)
        except SessionNotFound as exc:
            raise _not_found(exc) from exc
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def body() -> AsyncIterator[bytes]:
            async for e in host.service.stream(id, start, follow=follow):
                payload = json.dumps(event_json(e), default=str)
                yield f"id: {e.seq}\nevent: {e.kind}\ndata: {payload}\n\n".encode()

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @r.post("/sessions/{id}/pending/{pid}/approve")
    async def approve(id: str, pid: str) -> dict[str, object]:
        try:
            return pending_json(await host.service.approve(id, pid))
        except (SessionNotFound, PendingNotFound) as exc:
            raise _not_found(exc) from exc

    @r.post("/sessions/{id}/pending/{pid}/reject")
    async def reject(id: str, pid: str) -> dict[str, object]:
        try:
            return pending_json(await host.service.reject(id, pid))
        except (SessionNotFound, PendingNotFound) as exc:
            raise _not_found(exc) from exc

    return r


def build_tools(host: AgentsHost) -> Sequence[ToolDef]:
    def _guard(session_id: str, call: CallContext) -> None:
        # Terminal agents use the install token; a launched session may only touch itself.
        if call.session_id is not None and call.session_id != session_id:
            raise PermissionError("an agent session may only attach to itself")

    async def attach(args: BaseModel, call: CallContext) -> object:
        a = IdIn.model_validate(args.model_dump())
        _guard(a.id, call)
        info = await host.service.attach(a.id)
        return {
            "session": session_json(info.session),
            "pack_dir": str(info.pack_dir),
            "transcript": [event_json(e) for e in info.transcript],
        }

    async def note(args: BaseModel, call: CallContext) -> object:
        a = NoteIn.model_validate(args.model_dump())
        _guard(a.id, call)
        await host.service.append_external(a.id, "text", {"text": a.text})
        return {"ok": True}

    return [
        ToolDef(
            "agents_session_attach",
            "Continue a Jesinia chat session in this terminal: returns its subject, context "
            "pack directory, and transcript.",
            IdIn,
            attach,
            False,
        ),
        ToolDef(
            "agents_session_note",
            "Append your progress to an attached Jesinia session so the PWA shows it live.",
            NoteIn,
            note,
            False,
        ),
    ]
