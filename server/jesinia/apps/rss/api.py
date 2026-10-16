# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSS routes under ``/api/rss``: feeds, folders and OPML."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import APIRouter, Depends, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from jesinia.apps.rss.application.feeds import FeedService, ImportResult
from jesinia.apps.rss.domain.feed import (
    FOLDER_NAME_MAX,
    FeedExists,
    FeedNotFound,
    FeedUnavailable,
    FeedView,
    Folder,
    FolderExists,
    FolderNotFound,
    RssError,
)
from jesinia.core.idempotency import IdempotencyStore, IdempotentCall, idempotent

URL_MAX = 2048
OPML_MAX = 5_000_000
OPML_MEDIA_TYPE = "text/x-opml"


class RssHost(Protocol):
    """What the routes need from the module: the live service and the idempotency store."""

    @property
    def service(self) -> FeedService: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...


class FeedIn(BaseModel):
    url: str = Field(
        min_length=1, max_length=URL_MAX, description="A feed URL, or a site that links one"
    )
    folder: str | None = Field(default=None, max_length=FOLDER_NAME_MAX)


class FeedPatchIn(BaseModel):
    folder: str | None = Field(max_length=FOLDER_NAME_MAX, description="The folder; null for none")


class FolderPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=FOLDER_NAME_MAX)


class OpmlIn(BaseModel):
    opml: str = Field(min_length=1, max_length=OPML_MAX, description="The OPML document text")


class FeedOut(BaseModel):
    id: int
    title: str
    url: str
    folder: str | None
    folder_id: int | None
    unread: int
    warning: str | None

    @classmethod
    def of(cls, view: FeedView) -> FeedOut:
        return cls(
            id=view.id,
            title=view.title,
            url=view.url,
            folder=view.folder,
            folder_id=view.folder_id,
            unread=view.unread,
            warning=view.warning,
        )


class FolderOut(BaseModel):
    id: int
    name: str

    @classmethod
    def of(cls, folder: Folder) -> FolderOut:
        return cls(id=folder.id, name=folder.name)


class ImportFailureOut(BaseModel):
    url: str
    cause: str


class ImportResultOut(BaseModel):
    added: int
    existing: int
    failed: list[ImportFailureOut]

    @classmethod
    def of(cls, result: ImportResult) -> ImportResultOut:
        failed = [ImportFailureOut(url=f.url, cause=f.cause) for f in result.failed]
        return cls(added=result.added, existing=result.existing, failed=failed)


class RssErrorOut(BaseModel):
    error: str
    detail: str


_STATUS: dict[type[RssError], int] = {
    FeedNotFound: status.HTTP_404_NOT_FOUND,
    FolderNotFound: status.HTTP_404_NOT_FOUND,
    FeedExists: status.HTTP_409_CONFLICT,
    FolderExists: status.HTTP_409_CONFLICT,
    # The remote feed failed, not this server: the cause is in ``detail``.
    FeedUnavailable: status.HTTP_502_BAD_GATEWAY,
}

# ``Any`` is FastAPI's own type for ``responses`` (free-form OpenAPI fragments).
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": RssErrorOut} for code in (400, 404, 409, 502)
}


def _error(exc: RssError) -> JSONResponse:
    body = RssErrorOut(error=type(exc).__name__, detail=str(exc))
    code = _STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(body.model_dump(), status_code=code)


async def _respond(
    work: Callable[[], Awaitable[BaseModel | None]], ok: int = status.HTTP_200_OK
) -> Response:
    """``work``'s model as a concrete ``Response`` (so idempotency can store it), or the
    typed error as structured JSON."""
    try:
        result = await work()
    except RssError as exc:
        return _error(exc)
    if result is None:
        return Response(status_code=ok)
    return JSONResponse(jsonable_encoder(result), status_code=ok)


def build_router(host: RssHost) -> APIRouter:
    r = APIRouter(prefix="/api/rss", tags=["rss"], responses=_ERRORS)
    keyed = Depends(idempotent(lambda: host.idempotency))

    @r.get("/feeds", response_model=list[FeedOut])
    async def list_feeds() -> list[FeedOut]:
        return [FeedOut.of(v) for v in host.service.list()]

    @r.post("/feeds", status_code=status.HTTP_201_CREATED, response_model=FeedOut)
    async def add_feed(body: FeedIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> FeedOut:
            feed = await host.service.add(body.url, body.folder)
            return FeedOut.of(host.service.view(feed.id))

        return await idem.run(lambda: _respond(run, status.HTTP_201_CREATED))

    @r.delete("/feeds/{feed_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    async def remove_feed(feed_id: int, idem: IdempotentCall = keyed) -> Response:
        async def run() -> None:
            host.service.remove(feed_id)

        return await idem.run(lambda: _respond(run, status.HTTP_204_NO_CONTENT))

    @r.patch("/feeds/{feed_id}", response_model=FeedOut)
    async def move_feed(feed_id: int, body: FeedPatchIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> FeedOut:
            host.service.move(feed_id, body.folder)
            return FeedOut.of(host.service.view(feed_id))

        return await idem.run(lambda: _respond(run))

    @r.patch("/folders/{folder_id}", response_model=FolderOut)
    async def rename_folder(
        folder_id: int, body: FolderPatchIn, idem: IdempotentCall = keyed
    ) -> Response:
        async def run() -> FolderOut:
            return FolderOut.of(host.service.rename_folder(folder_id, body.name))

        return await idem.run(lambda: _respond(run))

    @r.post("/opml", response_model=ImportResultOut)
    async def import_opml(body: OpmlIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> ImportResultOut:
            return ImportResultOut.of(host.service.import_opml(body.opml))

        return await idem.run(lambda: _respond(run))

    @r.get(
        "/opml",
        response_class=Response,
        responses={200: {"content": {OPML_MEDIA_TYPE: {}}, "description": "The OPML file"}},
    )
    async def export_opml() -> Response:
        return Response(
            host.service.export_opml(),
            media_type=OPML_MEDIA_TYPE,
            headers={"Content-Disposition": 'attachment; filename="jesinia-feeds.opml"'},
        )

    return r
