# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/search`` and the ``search_query`` / ``kb_search`` MCP tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Protocol

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from jesinia.core.module import CallContext, ToolDef
from jesinia.services.search.application.service import KB_SOURCE, SearchHit, SearchService


class SearchHost(Protocol):
    @property
    def service(self) -> SearchService: ...


class HitOut(BaseModel):
    source: str
    key: str
    title: str
    snippet: str
    path: str | None
    url: str | None
    authoritative: bool
    score: float

    @classmethod
    def of(cls, h: SearchHit) -> HitOut:
        return cls(
            source=h.source,
            key=h.key,
            title=h.title,
            snippet=h.snippet,
            path=h.path,
            url=h.url,
            authoritative=h.authoritative,
            score=h.score,
        )


class QueryIn(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=200)
    sources: list[str] | None = None


class KbQueryIn(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=200)


def build_router(host: SearchHost) -> APIRouter:
    r = APIRouter(prefix="/api/search", tags=["search"])

    @r.get("")
    async def search(
        q: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        source: Annotated[list[str] | None, Query()] = None,
    ) -> list[HitOut]:
        hits = await host.service.query(
            q, limit=limit, sources=set(source) if source else None, hide_agent_only=True
        )
        return [HitOut.of(h) for h in hits]

    @r.post("/reindex")
    async def reindex() -> dict[str, int]:
        """Rebuild the KB part of the index (``jesinia reindex``)."""
        return {"indexed": await host.service.reindex()}

    return r


def build_tools(host: SearchHost) -> Sequence[ToolDef]:
    async def search_query(args: BaseModel, call: CallContext) -> object:
        a = QueryIn.model_validate(args.model_dump())
        hits = await host.service.query(
            a.q, limit=a.limit, sources=set(a.sources) if a.sources else None
        )
        return [HitOut.of(h).model_dump() for h in hits]

    async def kb_search(args: BaseModel, call: CallContext) -> object:
        a = KbQueryIn.model_validate(args.model_dump())
        hits = await host.service.query(a.q, limit=a.limit, sources={KB_SOURCE})
        return [HitOut.of(h).model_dump() for h in hits]

    return [
        ToolDef(
            "search_query",
            "Full-text search over KB pages and app records (e.g. RSS items).",
            QueryIn,
            search_query,
            False,
        ),
        ToolDef("kb_search", "Full-text search over KB pages only.", KbQueryIn, kb_search, False),
    ]
