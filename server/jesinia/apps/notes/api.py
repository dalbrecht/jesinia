# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notes routes and the ``notes_save`` MCP tool."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from jesinia.apps.notes.application.service import NotesService, NotSubjectSessionError
from jesinia.core.module import CallContext, ToolDef
from jesinia.services.agents.application.sessions import SessionNotFound
from jesinia.services.agents.domain.session import Session
from jesinia.services.kb.application.errors import KbError, PageNotFound
from jesinia.services.kb.application.service import PageWithRev
from jesinia.services.kb.domain.page import PageId


class NotesHost(Protocol):
    @property
    def service(self) -> NotesService: ...


class StartIn(BaseModel):
    page_id: str
    runner: str


class SaveIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    user_text: str = ""
    agent_text: str = Field(min_length=1)
    model: str = "unknown"


class NotesSaveIn(BaseModel):
    """`notes_save`: keep the agent's text as a note about the session's page."""

    title: str = Field(min_length=1, max_length=200, description="A short title for the note")
    text: str = Field(min_length=1, description="The text to keep (Markdown)")


def _session_json(s: Session) -> dict[str, object]:
    return {
        "id": s.id,
        "runner": s.runner,
        "status": s.status,
        "subject": {"kind": s.subject.kind, "ref": s.subject.ref, "title": s.subject.title},
    }


def _saved(n: PageWithRev) -> dict[str, object]:
    return {"id": n.page.id, "title": n.page.title, "path": str(n.page.path), "rev": n.rev}


def build_router(host: NotesHost) -> APIRouter:
    r = APIRouter(prefix="/api/notes", tags=["notes"])

    @r.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def start(body: StartIn) -> dict[str, object]:
        try:
            s = await host.service.start_session(PageId(body.page_id), body.runner)
        except PageNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:  # unknown runner
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _session_json(s)

    @r.post("/sessions/{id}/save", status_code=status.HTTP_201_CREATED)
    async def save(id: str, body: SaveIn) -> dict[str, object]:
        try:
            n = await host.service.save_note(
                id,
                title=body.title,
                user_text=body.user_text,
                agent_text=body.agent_text,
                model=body.model,
            )
        except SessionNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no session {id}") from exc
        except NotSubjectSessionError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except KbError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return _saved(n)

    return r


def build_tools(host: NotesHost) -> Sequence[ToolDef]:
    async def notes_save(args: BaseModel, call: CallContext) -> object:
        a = NotesSaveIn.model_validate(args.model_dump())
        if call.session_id is None:
            raise ValueError("notes_save works only inside a Notes session started from Jesinia")
        session = host.service.sessions.get(call.session_id)
        # An agent records only its own text; user-attributed text comes from the PWA.
        n = await host.service.save_note(
            call.session_id, title=a.title, user_text="", agent_text=a.text,
            model=session.model or "unknown", by_agent=True,
        )  # fmt: skip
        return _saved(n)

    return [
        ToolDef(
            "notes_save",
            "Save a note about the page this Notes session is about. The text is recorded as "
            "AI-written, with a link back to the page.",
            NotesSaveIn,
            notes_save,
            True,
        )
    ]
