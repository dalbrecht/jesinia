# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP routes (``/api/kb/…``) and MCP tool definitions over one ``KbService``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import PurePosixPath
from typing import Protocol

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jesinia.core.module import CallContext, ToolDef
from jesinia.services.kb.api.schemas import (
    BodyIn,
    ConflictOut,
    EmptyIn,
    IdIn,
    MoveIn,
    MoveOut,
    MoveToolIn,
    PageIn,
    PageOut,
    PathIn,
    TreeOut,
    WriteToolIn,
)
from jesinia.services.kb.application.errors import (
    KbError,
    KbUnavailable,
    PageNotFound,
    PathOutsideKb,
    ReservedPath,
    StaleWrite,
)
from jesinia.services.kb.application.service import KbService, PageDraft
from jesinia.services.kb.domain.page import PageId


class KbHost(Protocol):
    """What the routes need from the module: the live service, rescans, root changes."""

    @property
    def service(self) -> KbService: ...

    async def rescan(self) -> dict[str, object]: ...

    async def change_root(self, path: str) -> dict[str, object]: ...

    def root_info(self) -> dict[str, object]: ...


_STATUS: dict[type[KbError], int] = {
    KbUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    PageNotFound: status.HTTP_404_NOT_FOUND,
    StaleWrite: status.HTTP_409_CONFLICT,
    PathOutsideKb: status.HTTP_400_BAD_REQUEST,
    ReservedPath: status.HTTP_400_BAD_REQUEST,
}


def _http(exc: KbError) -> HTTPException:
    return HTTPException(_STATUS.get(type(exc), 400), f"{type(exc).__name__}: {exc}")


async def _guard[T](fn: Callable[[], Awaitable[T]]) -> T:
    try:
        return await fn()
    except KbError as exc:
        raise _http(exc) from exc


def build_router(host: KbHost) -> APIRouter:
    r = APIRouter(prefix="/api/kb", tags=["kb"])

    @r.get("/pages/{id}")
    async def get_page(id: str) -> PageOut:
        return PageOut.of(await _guard(lambda: host.service.read(PageId(id))))

    @r.put("/pages")
    async def put_page(body: PageIn) -> PageOut:
        draft = PageDraft(
            PageId(body.id) if body.id else None,
            body.title,
            PurePosixPath(body.folder),
            body.body,
            body.jesinia,
        )
        return PageOut.of(
            await _guard(lambda: host.service.write(draft, expected_rev=body.expected_rev))
        )

    @r.patch("/pages/{id}/body", response_model=None)
    async def patch_body(id: str, body: BodyIn) -> PageOut | JSONResponse:
        try:
            return PageOut.of(
                await host.service.update_body(
                    PageId(id), body.body, expected_rev=body.expected_rev
                )
            )
        except StaleWrite:
            current = await _guard(lambda: host.service.read(PageId(id)))
            return JSONResponse(
                {"error": "StaleWrite", "rev": current.rev, "body": current.page.body},
                status_code=status.HTTP_409_CONFLICT,
            )
        except KbError as exc:
            raise _http(exc) from exc

    @r.post("/pages/{id}/move")
    async def move_page(id: str, body: MoveIn) -> MoveOut:
        result = await _guard(
            lambda: host.service.move(PageId(id), PurePosixPath(body.folder), body.title)
        )
        return MoveOut.of(result)

    @r.get("/tree")
    async def get_tree() -> list[TreeOut]:
        return [TreeOut.of(n) for n in await _guard(host.service.tree)]

    @r.get("/conflicts")
    async def get_conflicts() -> list[ConflictOut]:
        return [ConflictOut.of(c) for c in await host.service.conflicts()]

    @r.post("/scan")
    async def post_scan() -> dict[str, object]:
        return await _guard(host.rescan)

    @r.get("/path")
    async def get_path() -> dict[str, object]:
        """The configured KB folder and, if it is unusable, why (Settings)."""
        return host.root_info()

    @r.put("/path")
    async def put_path(body: PathIn) -> dict[str, object]:
        return await host.change_root(body.path)

    return r


def build_tools(host: KbHost) -> Sequence[ToolDef]:
    async def kb_read(args: BaseModel, call: CallContext) -> object:
        a = IdIn.model_validate(args.model_dump())
        return PageOut.of(await host.service.read(PageId(a.id))).model_dump()

    async def kb_write(args: BaseModel, call: CallContext) -> object:
        a = WriteToolIn.model_validate(args.model_dump())
        draft = PageDraft(
            PageId(a.id) if a.id else None, a.title, PurePosixPath(a.folder), a.body, a.jesinia
        )
        return PageOut.of(await host.service.write(draft, expected_rev=a.expected_rev)).model_dump()

    async def kb_move(args: BaseModel, call: CallContext) -> object:
        a = MoveToolIn.model_validate(args.model_dump())
        result = await host.service.move(PageId(a.id), PurePosixPath(a.folder), a.title)
        return MoveOut.of(result).model_dump()

    async def preview_write(args: BaseModel) -> dict[str, object]:
        a = WriteToolIn.model_validate(args.model_dump())
        if a.id:
            current = await host.service.read(PageId(a.id))
            return {
                "action": "update",
                "path": str(current.page.path),
                "title": current.page.title,
                "current": current.page.body,
            }
        folder = PurePosixPath(a.folder)
        return {"action": "create", "path": str(folder / f"{a.title}.md"), "title": a.title}

    async def preview_move(args: BaseModel) -> dict[str, object]:
        a = MoveToolIn.model_validate(args.model_dump())
        current = await host.service.read(PageId(a.id))
        title = a.title or current.page.title
        return {
            "action": "move",
            "path": str(current.page.path),
            "to": str(PurePosixPath(a.folder) / f"{title}.md"),
        }

    async def kb_tree(args: BaseModel, call: CallContext) -> object:
        return [TreeOut.of(n).model_dump() for n in await host.service.tree()]

    async def kb_link(args: BaseModel, call: CallContext) -> object:
        a = IdIn.model_validate(args.model_dump())
        page = (await host.service.read(PageId(a.id))).page
        return {"id": page.id, "title": page.title, "path": str(page.path)}

    return [
        ToolDef("kb_read", "Read a KB page by id (body, front-matter, rev).", IdIn, kb_read, False),
        ToolDef(
            "kb_write",
            "Create a page (no id) or update one (id + expected_rev from kb_read).",
            WriteToolIn,
            kb_write,
            True,
            preview_write,
        ),
        ToolDef(
            "kb_move",
            "Move or rename a page; renames update link text.",
            MoveToolIn,
            kb_move,
            True,
            preview_move,
        ),
        ToolDef("kb_tree", "List KB folders and pages with page counts.", EmptyIn, kb_tree, False),
        ToolDef("kb_link", "Resolve a page id to its title and path.", IdIn, kb_link, False),
    ]
