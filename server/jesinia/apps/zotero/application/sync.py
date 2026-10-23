# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mirror the Zotero library locally and keep Jesinia's search index current (spec §4).

Each pass reads four feeds from the stored cursor and only commits the new cursor once all of
them succeeded, so a failure anywhere makes the next pass retry from the same place.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import cast

from jesinia.apps.zotero.application.ports import MirrorStore, ZoteroLibrary
from jesinia.apps.zotero.domain.identifiers import ItemKey, zotero_select_url
from jesinia.apps.zotero.domain.paper import Paper
from jesinia.services.search.application.service import ExternalRecord, SearchService

log = logging.getLogger(__name__)

SEARCH_SOURCE = "zotero"
SEARCH_BODY_LIMIT = 200_000
_PDF = "application/pdf"
_YEAR = re.compile(r"\d{4}")
_SKIPPED_HTML = frozenset({"script", "style"})
_BLOCK_SEPARATOR = "\n\n"

PaperParser = Callable[[dict[str, object], list[dict[str, object]]], Paper]
KeyHook = Callable[[ItemKey], Awaitable[None]]
ServerChangedHook = Callable[[str], Awaitable[None]]
MovedHook = Callable[[ItemKey, ItemKey], Awaitable[None]]


@dataclass(frozen=True)
class SyncReport:
    """What one pass did: papers re-mirrored, papers dropped, papers whose full text refreshed."""

    changed: int
    removed: int
    fulltext_updated: int


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_HTML:
            self._skip_depth += 1
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_HTML and self._skip_depth:
            self._skip_depth -= 1
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Plain text of Zotero's note markup, with whitespace collapsed."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return " ".join("".join(extractor.parts).split())


def _title(paper: Paper) -> str:
    author = next((c.last for c in paper.creators if c.last), "")
    year_match = _YEAR.search(paper.date)
    byline = " ".join(part for part in (author, year_match.group() if year_match else "") if part)
    if paper.title and byline:
        return f"{paper.title} — {byline}"
    return paper.title or byline


def search_record(paper: Paper, fulltext: str | None) -> ExternalRecord:
    """The search entry for a paper: title with author and year, body from abstract, tags,
    notes, annotations, then full text, capped at ``SEARCH_BODY_LIMIT``."""
    blocks = [paper.abstract, " ".join(tag.name for tag in paper.tags)]
    blocks += [html_to_text(note.html) for note in paper.notes]
    for attachment in paper.attachments:
        for annotation in attachment.annotations:
            blocks += [annotation.text, annotation.comment]
    blocks.append(fulltext or "")
    body = _BLOCK_SEPARATOR.join(block for block in blocks if block)
    return ExternalRecord(
        SEARCH_SOURCE,
        paper.key,
        _title(paper),
        body[:SEARCH_BODY_LIMIT],
        zotero_select_url(paper.key),
    )


async def _noop_key(key: ItemKey) -> None:
    return None


async def _noop_moved(old_key: ItemKey, parent_key: ItemKey) -> None:
    return None


async def _noop_server(old_server_id: str) -> None:
    return None


def _data(raw: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], raw["data"])


def _is_pdf(data: dict[str, object]) -> bool:
    return data.get("itemType") == "attachment" and data.get("contentType") == _PDF


def _is_paper(data: dict[str, object]) -> bool:
    """A top-level item that is a paper: not a note, annotation or non-PDF attachment."""
    item_type = data.get("itemType")
    if item_type == "attachment":
        return _is_pdf(data)
    return item_type not in ("note", "annotation")


class SyncService:
    """Keeps the mirror and the search index in step with Zotero."""

    def __init__(
        self,
        library: ZoteroLibrary,
        mirror: MirrorStore,
        search: SearchService,
        on_new: KeyHook = _noop_key,
        on_gone: KeyHook = _noop_key,
        on_server_changed: ServerChangedHook = _noop_server,
        *,
        on_moved: MovedHook = _noop_moved,
        parse: PaperParser,
    ) -> None:
        """``parse`` turns a raw item and its descendants into a Paper (infrastructure's
        ``parse_paper``); it is injected so this layer does not import infrastructure.

        ``on_moved(old_key, parent_key)`` fires when a standalone PDF gains a parent.

        The mirror write is always the last step for a paper, so a failure in search or in a
        hook makes the next pass redo that paper. Hooks must therefore tolerate being called
        again for the same key."""
        self._parse = parse
        self._library = library
        self._mirror = mirror
        self._search = search
        self._on_new = on_new
        self._on_gone = on_gone
        self._on_server_changed = on_server_changed
        self._on_moved = on_moved
        self._lock = asyncio.Lock()

    async def reregister_all(self) -> None:
        """Put every mirrored paper back into search; ``search.sqlite`` is derived and may be
        gone while the mirror survived."""
        for key in self._mirror.all_keys():
            paper = self._mirror.get(key)
            if paper is not None:
                await self._search.register(search_record(paper, self._mirror.fulltext(key)))

    async def sync(self) -> SyncReport:
        """One pass. Raises on any feed failure, leaving the cursor where it was."""
        async with self._lock:
            return await self._sync()

    async def _reset_for_new_server(self, old_server_id: str) -> None:
        for key in self._mirror.all_keys():
            await self._search.unregister(SEARCH_SOURCE, key)
        self._mirror.clear()
        await self._on_server_changed(old_server_id)

    async def _root_key(self, key: ItemKey, known: dict[ItemKey, ItemKey | None]) -> ItemKey:
        """The top-level item above ``key`` (itself when parentless), cached in ``known``."""
        if key not in known:
            raw = await self._library.item(key)
            parent = _data(raw).get("parentItem")
            known[key] = ItemKey(str(parent)) if parent else None
        parent_key = known[key]
        return key if parent_key is None else await self._root_key(parent_key, known)

    async def _sync(self) -> SyncReport:
        # The client caches the server id from its last response, so make a request first:
        # that is what notices a library swapped out from under us.
        await self._library.library_version()
        server_id = await self._library.server_id()
        stored_server_id = self._mirror.server_id()
        if stored_server_id is not None and stored_server_id != server_id:
            await self._reset_for_new_server(stored_server_id)
        self._mirror.set_server_id(server_id)
        cursor = self._mirror.library_version()

        items, new_cursor = await self._library.changed_since(cursor)
        trashed = await self._library.trashed_since(cursor)
        live = await self._library.live_keys()
        # Full-text versions are read from 0 and compared per paper with the mirror: the
        # local API's full-text versions need not share the item-version counter.
        fulltext_versions = await self._library.fulltext_versions(0)

        removed = await self._drop_gone(set(trashed) | (set(self._mirror.all_keys()) - live))

        raw_by_key = {ItemKey(str(raw["key"])): raw for raw in items}
        known_parents: dict[ItemKey, ItemKey | None] = {
            key: (ItemKey(str(parent)) if (parent := _data(raw).get("parentItem")) else None)
            for key, raw in raw_by_key.items()
        }
        await self._drop_moved(raw_by_key)
        to_sync: dict[ItemKey, None] = {}
        for key, raw in raw_by_key.items():
            root = await self._root_key(key, known_parents)
            if root in live:
                to_sync[root] = None
        if cursor:
            # A trashed child (note, annotation) changes its parent's record.
            for key in trashed:
                if self._mirror.get(key) is None and key not in raw_by_key:
                    root = await self._root_key(key, known_parents)
                    if root in live:
                        to_sync[root] = None

        changed = 0
        for key in to_sync:
            if await self._mirror_paper(key, raw_by_key.get(key), fulltext_versions):
                changed += 1

        refreshed = await self._refresh_fulltext(fulltext_versions, set(to_sync))
        self._mirror.set_library_version(new_cursor)
        return SyncReport(changed=changed, removed=removed, fulltext_updated=refreshed)

    async def _drop_moved(self, raw_by_key: dict[ItemKey, dict[str, object]]) -> None:
        """A standalone PDF dragged under a paper stops being a paper of its own."""
        for key, raw in raw_by_key.items():
            parent = _data(raw).get("parentItem")
            if parent and self._mirror.get(key) is not None:
                await self._search.unregister(SEARCH_SOURCE, key)
                await self._on_moved(key, ItemKey(str(parent)))
                self._mirror.remove(key)

    async def _drop_gone(self, gone: set[ItemKey]) -> int:
        removed = 0
        for key in sorted(gone):
            if self._mirror.get(key) is None:
                continue
            await self._search.unregister(SEARCH_SOURCE, key)
            await self._on_gone(key)
            self._mirror.remove(key)
            removed += 1
        return removed

    async def _mirror_paper(
        self,
        key: ItemKey,
        raw: dict[str, object] | None,
        fulltext_versions: dict[ItemKey, int],
    ) -> bool:
        """Re-fetch one paper with its children and full text; False if it is not a paper."""
        raw = raw if raw is not None else await self._library.item(key)
        data = _data(raw)
        if data.get("parentItem") or not _is_paper(data):
            return False
        children = await self._library.children(key)
        for child in list(children):
            if _is_pdf(_data(child)):
                children += await self._library.children(ItemKey(str(child["key"])))
        paper = self._parse(raw, children)
        is_new = self._mirror.get(key) is None
        fulltext, versions = await self._fetch_fulltext(paper, fulltext_versions)
        await self._search.register(search_record(paper, fulltext))
        if is_new:
            await self._on_new(key)
        self._mirror.put(paper, fulltext, versions)
        return True

    async def _fetch_fulltext(
        self, paper: Paper, fulltext_versions: dict[ItemKey, int]
    ) -> tuple[str | None, dict[ItemKey, int]]:
        """The PDFs' text joined, and the full-text version seen per PDF (even with no text)."""
        texts: list[str] = []
        for attachment in paper.attachments:
            text = await self._library.fulltext(attachment.key)
            if text:
                texts.append(text)
        versions = {a.key: fulltext_versions.get(a.key, 0) for a in paper.attachments}
        return (_BLOCK_SEPARATOR.join(texts) or None), versions

    async def _refresh_fulltext(
        self, fulltext_versions: dict[ItemKey, int], already_synced: set[ItemKey]
    ) -> int:
        """Re-fetch full text for papers with a PDF indexed since we last looked."""
        stale: dict[ItemKey, None] = {}
        for attachment_key, version in fulltext_versions.items():
            owner = self._mirror.owner_of(attachment_key)
            if (
                owner is not None
                and owner not in already_synced
                and version > self._mirror.fulltext_version(attachment_key)
            ):
                stale[owner] = None
        refreshed = 0
        for owner in sorted(stale):
            paper = self._mirror.get(owner)
            if paper is None:  # pragma: no cover - owner_of only returns mirrored papers
                continue
            fulltext, versions = await self._fetch_fulltext(paper, fulltext_versions)
            await self._search.register(search_record(paper, fulltext))
            self._mirror.put(paper, fulltext, versions)
            refreshed += 1
        return refreshed
