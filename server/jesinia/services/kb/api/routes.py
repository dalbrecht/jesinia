# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP routes (``/api/kb/…``) and MCP tool definitions over one ``KbService``.

The routes are the user's view: they neither list nor open agent-only pages (ADR 0011).
The MCP tools are the agents' view and reach every page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Protocol

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse, Response
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
    AgentOnlyPage,
    AssetNotFound,
    KbError,
    KbUnavailable,
    PageNotFound,
    PathOutsideKb,
    ReservedPath,
    StaleWrite,
    UnsupportedAssetType,
)
from jesinia.services.kb.application.service import KbService, PageDraft
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.provenance import inert, provenance_label, wrap_ai


class KbHost(Protocol):
    """What the routes need from the module: the live service, rescans, root changes."""

    @property
    def service(self) -> KbService: ...

    async def rescan(self) -> dict[str, object]: ...

    async def change_root(self, path: str) -> dict[str, object]: ...

    def root_info(self) -> dict[str, object]: ...


_STATUS: dict[type[KbError], int] = {
    AgentOnlyPage: status.HTTP_403_FORBIDDEN,
    KbUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    PageNotFound: status.HTTP_404_NOT_FOUND,
    StaleWrite: status.HTTP_409_CONFLICT,
    PathOutsideKb: status.HTTP_400_BAD_REQUEST,
    ReservedPath: status.HTTP_400_BAD_REQUEST,
    AssetNotFound: status.HTTP_404_NOT_FOUND,
    UnsupportedAssetType: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
}
# Asset names are content hashes, but a name can be rewritten in place: cache for a while,
# in this browser only (the bytes are the user's).
ASSET_CACHE = "private, max-age=3600"
_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif")


def _http(exc: KbError) -> HTTPException:
    return HTTPException(_STATUS.get(type(exc), 400), f"{type(exc).__name__}: {exc}")


async def _guard[T](fn: Callable[[], Awaitable[T]]) -> T:
    try:
        return await fn()
    except KbError as exc:
        raise _http(exc) from exc


def build_router(host: KbHost) -> APIRouter:
    r = APIRouter(prefix="/api/kb", tags=["kb"])

    def readable(id: str) -> PageId:
        """``id`` once it is known not to be agent-only (else 403)."""
        try:
            host.service.require_user_access(PageId(id))
        except KbError as exc:
            raise _http(exc) from exc
        return PageId(id)

    def writable_folder(folder: str) -> PurePosixPath:
        """``folder`` once it is known not to be agent-only (else 403)."""
        try:
            host.service.require_user_folder(PurePosixPath(folder))
        except KbError as exc:
            raise _http(exc) from exc
        return PurePosixPath(folder)

    @r.get("/pages/{id}")
    async def get_page(id: str) -> PageOut:
        page_id = readable(id)
        return PageOut.of(await _guard(lambda: host.service.read(page_id)))

    @r.get(
        "/assets/{page_id}/{file}",
        response_class=Response,
        responses={200: {"content": {t: {} for t in _IMAGE_TYPES}}},
    )
    async def get_asset(page_id: str, file: str) -> Response:
        """A page's local image (``_assets/<page_id>/<file>``), typed by its bytes: raster
        images only (415 for anything else, SVG included), never through a symlink."""
        readable(page_id)
        asset = await _guard(lambda: host.service.read_asset(page_id, file))
        return Response(
            asset.data,
            media_type=asset.media_type,
            headers={"Cache-Control": ASSET_CACHE, "X-Content-Type-Options": "nosniff"},
        )

    @r.put("/pages")
    async def put_page(body: PageIn) -> PageOut:
        draft = PageDraft(
            readable(body.id) if body.id else None,
            body.title,
            writable_folder(body.folder),
            body.body,
            body.jesinia,
        )
        return PageOut.of(
            await _guard(lambda: host.service.write(draft, expected_rev=body.expected_rev))
        )

    @r.patch("/pages/{id}/body", response_model=None)
    async def patch_body(id: str, body: BodyIn) -> PageOut | JSONResponse:
        readable(id)
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
        page_id, folder = readable(id), writable_folder(body.folder)
        result = await _guard(lambda: host.service.move(page_id, folder, body.title))
        return MoveOut.of(result)

    @r.get("/tree")
    async def get_tree() -> list[TreeOut]:
        nodes = await _guard(lambda: host.service.tree(hide_agent_only=True))
        return [TreeOut.of(n) for n in nodes]

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
        body = a.body
        if a.provenance is not None:
            if a.id:
                # Wrapping a whole existing body would claim the user's words as AI-written.
                raise ValueError("provenance applies to new pages only; omit it on updates")
            body = wrap_ai(
                inert(body),
                agent=provenance_label(a.provenance.agent),
                model=provenance_label(a.provenance.model),
                at=datetime.now(UTC),
                session=call.session_id or "terminal",
            )
        jesinia = a.jesinia
        if jesinia is None:
            # An omitted `jesinia` must not wipe kind/captured on update: keep what is there.
            jesinia = dict((await host.service.read(PageId(a.id))).page.jesinia) if a.id else {}
        draft = PageDraft(
            PageId(a.id) if a.id else None, a.title, PurePosixPath(a.folder), body, jesinia
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
            to, _ = host.service.planned_path(PurePosixPath(a.folder), a.title, PageId(a.id))
            preview: dict[str, object] = {
                "action": "update",
                "path": str(current.page.path),
                "title": current.page.title,
                "current": current.page.body,
            }
            # kb_write also retitles/moves when title or folder differ: say so.
            if a.title != current.page.title or PurePosixPath(a.folder) != current.page.path.parent:
                preview.update(action="update+move", to=str(to), new_title=a.title)
            if a.jesinia is not None and a.jesinia != current.page.jesinia:
                preview["jesinia"] = {"from": current.page.jesinia, "to": a.jesinia}
            return preview
        path, suffixed = host.service.planned_path(PurePosixPath(a.folder), a.title)
        created: dict[str, object] = {"action": "create", "path": str(path), "title": a.title}
        if a.provenance is not None:
            created["provenance"] = {"agent": a.provenance.agent, "model": a.provenance.model}
        if suffixed:
            created["note"] = "That name is taken; the new page's id will be added to it."
        return created

    async def preview_move(args: BaseModel) -> dict[str, object]:
        a = MoveToolIn.model_validate(args.model_dump())
        current = await host.service.read(PageId(a.id))
        title = a.title or current.page.title
        to, _ = host.service.planned_path(PurePosixPath(a.folder), title, PageId(a.id))
        return {"action": "move", "path": str(current.page.path), "to": str(to)}

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
            "Create a page (no id) or update one (id + expected_rev from kb_read). Set "
            "provenance when you wrote a new page's body.",
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
