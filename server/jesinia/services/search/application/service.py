# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SearchService``: full-text search over KB pages plus app-registered records (spec §4.2)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from jesinia.core.events import Event, EventBus
from jesinia.services.kb.application.errors import KbError
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.domain.page import PageId

log = logging.getLogger(__name__)

KB_SOURCE = "kb"
_TERM = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class ExternalRecord:
    source: str
    key: str
    title: str
    body: str
    url: str | None


@dataclass(frozen=True)
class SearchHit:
    source: str
    key: str
    title: str
    snippet: str  # HTML-escaped text with <mark>…</mark> around matches
    path: str | None
    authoritative: bool
    score: float
    url: str | None = None


@dataclass(frozen=True)
class IndexedDoc:
    source: str
    key: str
    title: str
    body: str
    url: str | None
    path: str | None
    authoritative: bool
    meta: str = ""  # searchable metadata (a KB page's `jesinia:` block, flattened)


def flatten_meta(value: object) -> str:
    """``{"kind": "note", "tags": ["a"]}`` → ``"kind note tags a"`` for full-text search."""
    if isinstance(value, dict):
        return " ".join(f"{k} {flatten_meta(v)}".strip() for k, v in value.items())
    if isinstance(value, list | tuple):
        return " ".join(flatten_meta(v) for v in value)
    return "" if value is None else str(value)


class SearchIndex(Protocol):
    def upsert(self, doc: IndexedDoc) -> None: ...

    def delete(self, source: str, key: str) -> None: ...

    def clear_source(self, source: str) -> None: ...

    def match(
        self, fts_query: str, limit: int, sources: Sequence[str] | None
    ) -> list[SearchHit]: ...


def to_fts_query(q: str) -> str | None:
    """User text → a safe FTS5 query: every term quoted as a phrase; last term is a prefix."""
    terms = _TERM.findall(q)
    if not terms:
        return None
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] += "*"
    return " ".join(quoted)


class SearchService:
    def __init__(self, index: SearchIndex, kb: KbService) -> None:
        self._index = index
        self._kb = kb
        self.indexing = False

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe("kb.page_changed", self._on_page_changed)
        bus.subscribe("kb.root_changed", self._on_root_changed)

    async def query(
        self, q: str, *, limit: int = 20, sources: set[str] | None = None
    ) -> list[SearchHit]:
        fts = to_fts_query(q)
        if fts is None:
            return []
        return self._index.match(fts, max(1, min(limit, 200)), sorted(sources) if sources else None)

    async def register(self, rec: ExternalRecord) -> None:
        if rec.source == KB_SOURCE:
            raise ValueError("source 'kb' is reserved for KB pages")
        self._index.upsert(
            IndexedDoc(rec.source, rec.key, rec.title, rec.body, rec.url, None, False)
        )

    async def unregister(self, source: str, key: str) -> None:
        self._index.delete(source, key)

    async def _index_page(self, id: PageId) -> None:
        try:
            p = await self._kb.read(id)
        except KbError:
            self._index.delete(KB_SOURCE, id)
            return
        self._index.upsert(
            IndexedDoc(
                KB_SOURCE,
                id,
                p.page.title,
                p.page.body,
                None,
                str(p.page.path),
                True,
                flatten_meta(p.page.jesinia),
            )
        )

    async def reindex(self) -> int:
        """Rebuild the KB part of the index; registered records are kept."""
        self.indexing = True
        try:
            self._index.clear_source(KB_SOURCE)
            count = 0
            for row in self._kb.index.all():
                await self._index_page(row.id)
                count += 1
                await asyncio.sleep(0)  # stay responsive while indexing large KBs
            return count
        finally:
            self.indexing = False

    async def _on_page_changed(self, e: Event) -> None:
        id = PageId(str(e.payload["id"]))
        if e.payload.get("change") == "missing":
            self._index.delete(KB_SOURCE, id)
        else:
            await self._index_page(id)

    async def _on_root_changed(self, e: Event) -> None:
        await self.reindex()
