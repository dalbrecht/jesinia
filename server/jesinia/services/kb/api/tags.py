# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tag catalog routes (``/api/kb/tags/…``) and the read-only ``tags_*`` MCP tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jesinia.core.module import CallContext, ToolDef
from jesinia.services.kb.api.schemas import (
    TAG_MAX,
    EmptyIn,
    MergeIn,
    RenameIn,
    RenamePreviewIn,
    RenamePreviewOut,
    RenameResultOut,
    SuggestIn,
    SuggestionOut,
    TagCheckOut,
    TagErrorOut,
    TagIn,
    TagInfoOut,
    TagNameIn,
    UncatalogedOut,
)
from jesinia.services.kb.application.errors import (
    InvalidTag,
    KbError,
    KbUnavailable,
    NearDuplicate,
    PageNotFound,
    StaleWrite,
    TagExists,
    TagNotFound,
)
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.domain.page import PageId


class TagsHost(Protocol):
    """What the tag routes need from the module: the live ``TagService``."""

    @property
    def tags(self) -> TagService: ...


_STATUS: dict[type[KbError], int] = {
    KbUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    PageNotFound: status.HTTP_404_NOT_FOUND,
    StaleWrite: status.HTTP_409_CONFLICT,
    InvalidTag: status.HTTP_400_BAD_REQUEST,
    TagExists: status.HTTP_409_CONFLICT,
    NearDuplicate: status.HTTP_409_CONFLICT,
    TagNotFound: status.HTTP_404_NOT_FOUND,
}

# Documented error bodies, so the typed client knows the 409's ``near`` list. ``Any`` is
# FastAPI's own type for ``responses`` (free-form OpenAPI fragments).
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": TagErrorOut} for code in (400, 404, 409, 503)
}


def _error(exc: KbError) -> JSONResponse:
    body = TagErrorOut(error=type(exc).__name__, detail=str(exc))
    if isinstance(exc, NearDuplicate):
        body.near = list(exc.near)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=_STATUS.get(type(exc), 400))


async def _answer[T](fn: Callable[[], Awaitable[T]]) -> T | JSONResponse:
    try:
        return await fn()
    except KbError as exc:
        return _error(exc)


def _revs(revs: dict[str, str]) -> dict[PageId, str]:
    return {PageId(page): rev for page, rev in revs.items()}


def build_tags_router(host: TagsHost) -> APIRouter:
    """Routes under ``/tags``; ``KbModule`` mounts them inside the ``/api/kb`` router."""
    r = APIRouter(prefix="/tags", tags=["kb"], responses=_ERRORS)

    @r.get("", response_model=list[TagInfoOut])
    async def list_tags() -> list[TagInfoOut] | JSONResponse:
        async def run() -> list[TagInfoOut]:
            return [TagInfoOut.of(t) for t in await host.tags.catalog()]

        return await _answer(run)

    @r.post("", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    async def add_tag(body: TagIn) -> JSONResponse | None:
        return await _answer(lambda: host.tags.add(body.tag, body.meaning, force=body.force))

    @r.get("/check", response_model=TagCheckOut)
    async def check_tag(
        tag: str = Query(min_length=1, max_length=TAG_MAX),
    ) -> TagCheckOut | JSONResponse:
        async def run() -> TagCheckOut:
            return TagCheckOut.of(await host.tags.check(tag))

        return await _answer(run)

    @r.post("/suggest", response_model=list[SuggestionOut])
    async def suggest_tags(body: SuggestIn) -> list[SuggestionOut] | JSONResponse:
        async def run() -> list[SuggestionOut]:
            found = await host.tags.suggest(body.text, limit=body.limit)
            return [SuggestionOut.of(s) for s in found]

        return await _answer(run)

    @r.get("/uncataloged", response_model=list[UncatalogedOut])
    async def uncataloged_tags() -> list[UncatalogedOut] | JSONResponse:
        async def run() -> list[UncatalogedOut]:
            found = await host.tags.uncataloged()
            return [UncatalogedOut(tag=tag, count=count) for tag, count in found]

        return await _answer(run)

    @r.post("/rename/preview", response_model=RenamePreviewOut)
    async def preview_tag_rename(body: RenamePreviewIn) -> RenamePreviewOut | JSONResponse:
        async def run() -> RenamePreviewOut:
            return RenamePreviewOut.of(await host.tags.preview_rename(body.old, body.new))

        return await _answer(run)

    @r.post("/rename", response_model=RenameResultOut)
    async def rename_tag(body: RenameIn) -> RenameResultOut | JSONResponse:
        async def run() -> RenameResultOut:
            result = await host.tags.apply_rename(body.old, body.new, _revs(body.revs))
            return RenameResultOut.of(result)

        return await _answer(run)

    @r.post("/merge", response_model=RenameResultOut)
    async def merge_tags(body: MergeIn) -> RenameResultOut | JSONResponse:
        async def run() -> RenameResultOut:
            result = await host.tags.merge(body.sources, body.into, _revs(body.revs))
            return RenameResultOut.of(result)

        return await _answer(run)

    @r.post("/retire", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    async def retire_tag(body: TagNameIn) -> JSONResponse | None:
        return await _answer(lambda: host.tags.retire(body.tag))

    @r.post("/adopt", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    async def adopt_tag(body: TagNameIn) -> JSONResponse | None:
        return await _answer(lambda: host.tags.adopt(body.tag))

    @r.post("/ignore", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    async def ignore_tag(body: TagNameIn) -> JSONResponse | None:
        return await _answer(lambda: host.tags.ignore(body.tag))

    return r


def build_tags_tools(host: TagsHost) -> Sequence[ToolDef]:
    """Read-only tools: creating, renaming or retiring a tag stays a user action (§5.5)."""

    async def tags_list(args: BaseModel, call: CallContext) -> object:
        return [TagInfoOut.of(t).model_dump() for t in await host.tags.catalog()]

    async def tags_suggest(args: BaseModel, call: CallContext) -> object:
        a = SuggestIn.model_validate(args.model_dump())
        found = await host.tags.suggest(a.text, limit=a.limit)
        return [SuggestionOut.of(s).model_dump() for s in found]

    async def tags_check(args: BaseModel, call: CallContext) -> object:
        a = TagNameIn.model_validate(args.model_dump())
        return TagCheckOut.of(await host.tags.check(a.tag)).model_dump()

    return [
        ToolDef(
            "tags_list",
            "List the KB tag catalog: each tag's meaning, aliases, retired flag and page count.",
            EmptyIn,
            tags_list,
            False,
        ),
        ToolDef(
            "tags_suggest",
            "Suggest tags for a text: catalog tags first; a tag marked new is not in the "
            "catalog, and only the user can add it.",
            SuggestIn,
            tags_suggest,
            False,
        ),
        ToolDef(
            "tags_check",
            "Normalise a tag and check it against the catalog (exact, alias and near matches).",
            TagNameIn,
            tags_check,
            False,
        ),
    ]
