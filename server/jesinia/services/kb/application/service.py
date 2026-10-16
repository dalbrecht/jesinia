# SPDX-License-Identifier: AGPL-3.0-or-later
"""``KbService``: read, write, update, move, and browse pages (spec §4.1)."""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from jesinia.core.events import Event, EventBus
from jesinia.services.kb.application.errors import (
    KbUnavailable,
    PageNotFound,
    PathOutsideKb,
    ReservedPath,
    StaleWrite,
)
from jesinia.services.kb.application.ports import PageFiles, PageIndex
from jesinia.services.kb.domain.frontmatter import (
    page_from,
    parse_document,
    replace_body,
    serialize_page,
)
from jesinia.services.kb.domain.links import link_targets, retitle_links
from jesinia.services.kb.domain.page import Page, PageId, new_page_id
from jesinia.services.kb.domain.paths import filename_for, is_hidden, is_reserved

Change = Literal["created", "updated", "moved", "missing"]


@dataclass(frozen=True)
class PageDraft:
    id: PageId | None
    title: str
    folder: PurePosixPath
    body: str
    jesinia: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PageWithRev:
    page: Page
    rev: str  # sha256 hex of the file bytes


@dataclass(frozen=True)
class MoveResult:
    page: PageWithRev
    relinked: list[PageId]  # linking pages whose [[id|Title]] text was updated
    skipped: list[PageId] = field(default_factory=list)  # changed since indexed; left alone


@dataclass(frozen=True)
class TreeNode:
    path: PurePosixPath
    kind: Literal["folder", "page"]
    title: str | None = None
    id: PageId | None = None
    pages: int = 0
    children: list[TreeNode] = field(default_factory=list)


def nfc(path: PurePosixPath | str) -> PurePosixPath:
    return PurePosixPath(unicodedata.normalize("NFC", str(path)))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class KbService:
    def __init__(self, files: PageFiles | None, index: PageIndex, bus: EventBus) -> None:
        self._files = files
        self._index = index
        self._bus = bus
        self._locks: defaultdict[PageId, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- helpers -------------------------------------------------------------------
    @property
    def root(self) -> Path | None:
        return self._files.root if self._files is not None else None

    def _fs(self) -> PageFiles:
        if self._files is None:
            raise KbUnavailable("no KB path is configured")
        return self._files

    def resolve(self, id: PageId) -> PurePosixPath | None:
        row = self._index.get(id)
        return row.path if row else None

    def _check_folder(self, folder: PurePosixPath) -> PurePosixPath:
        if folder.is_absolute() or ".." in folder.parts:
            raise PathOutsideKb(f"{folder}: must be a relative path inside the KB")
        probe = folder / "x.md"
        if is_reserved(probe) or is_hidden(probe):
            raise ReservedPath(f"{folder}: Jesinia never writes in reserved or hidden folders")
        self._fs().contain(folder)
        return nfc(folder)

    def _target(self, folder: PurePosixPath, title: str, id: PageId) -> PurePosixPath:
        siblings = [(r.title, r.id) for r in self._index.all() if r.path.parent == folder]
        path = folder / filename_for(title, id, siblings)
        existing = self._index.by_path(path)
        fs = self._fs()
        if (existing and existing.id != id) or (existing is None and fs.exists(str(path))):
            stem = path.stem
            path = folder / f"{stem} [{id.replace('-', '')[:8]}].md"
        self._fs().contain(path)
        return path

    async def _emit(self, id: PageId, path: PurePosixPath, change: Change) -> None:
        payload = {"id": id, "path": str(path), "change": change, "origin": "jesinia"}
        await self._bus.publish(Event("kb.page_changed", payload))

    def _store(self, page: Page, disk: str, data: bytes) -> PageWithRev:
        mtime = self._fs().write_atomic(disk, data)
        rev = sha256(data)
        self._index.put(page.id, page.path, disk, rev, page.title, mtime)
        self._index.set_links(page.id, link_targets(page.body))
        return PageWithRev(page, rev)

    def _current(self, id: PageId) -> tuple[str, bytes]:
        row = self._index.get(id)
        if row is None:
            raise PageNotFound(id)
        try:
            return row.disk, self._fs().read(row.disk)
        except FileNotFoundError as exc:
            raise PageNotFound(f"{id}: {row.path} is missing") from exc

    # -- reads ---------------------------------------------------------------------
    async def read(self, id: PageId) -> PageWithRev:
        self._fs()
        disk, data = self._current(id)
        row = self._index.get(id)
        assert row is not None
        page = page_from(parse_document(data.decode("utf-8")), row.path)
        return PageWithRev(page, sha256(data))

    async def tree(self) -> list[TreeNode]:
        self._fs()
        root = TreeNode(PurePosixPath(""), "folder")
        folders: dict[PurePosixPath, TreeNode] = {PurePosixPath(""): root}

        def folder(path: PurePosixPath) -> TreeNode:
            if path not in folders:
                node = TreeNode(path, "folder")
                folders[path] = node
                folder(path.parent).children.append(node)
            return folders[path]

        for row in self._index.all():
            if is_reserved(row.path) or is_hidden(row.path):
                continue
            folder(row.path.parent).children.append(
                TreeNode(row.path, "page", title=row.title, id=row.id, pages=1)
            )

        def finish(node: TreeNode) -> TreeNode:
            kids = [finish(c) if c.kind == "folder" else c for c in node.children]
            kids.sort(key=lambda c: (c.kind != "folder", (c.title or c.path.name).casefold()))
            return replace(node, children=kids, pages=sum(c.pages for c in kids))

        return finish(root).children

    # -- writes --------------------------------------------------------------------
    async def write(self, draft: PageDraft, *, expected_rev: str | None = None) -> PageWithRev:
        """Create a page (``id=None``) or rewrite one in canonical form.

        Updating an existing page requires ``expected_rev``; a mismatch (or none given)
        raises ``StaleWrite`` and leaves the file untouched (spec review focus 2).
        """
        folder = self._check_folder(draft.folder)
        if draft.id is None:
            id = new_page_id()
            async with self._locks[id]:
                path = self._target(folder, draft.title, id)
                page = Page(id, draft.title, path, draft.body, dict(draft.jesinia), {})
                result = self._store(page, str(path), serialize_page(page).encode())
            await self._emit(id, path, "created")
            return result

        id = draft.id
        async with self._locks[id]:
            disk, data = self._current(id)
            if expected_rev is None or sha256(data) != expected_rev:
                raise StaleWrite(f"{id}: changed since read (or no base revision given)")
            row = self._index.get(id)
            assert row is not None
            current = page_from(parse_document(data.decode("utf-8")), row.path)
            moved = draft.title != current.title or folder != row.path.parent
            path = self._target(folder, draft.title, id) if moved else row.path
            if path != row.path:
                self._fs().contain(path.parent)
                self._fs().rename(disk, str(path))
                disk = str(path)
            page = Page(id, draft.title, path, draft.body, dict(draft.jesinia), current.foreign)
            result = self._store(page, disk, serialize_page(page).encode())
        await self._emit(id, path, "moved" if path != row.path else "updated")
        return result

    async def update_body(self, id: PageId, body: str, *, expected_rev: str) -> PageWithRev:
        """Replace only the body; the raw front-matter block stays byte-identical."""
        self._fs()
        async with self._locks[id]:
            disk, data = self._current(id)
            if sha256(data) != expected_rev:
                raise StaleWrite(f"{id}: changed since read")
            row = self._index.get(id)
            assert row is not None
            text = replace_body(data.decode("utf-8"), body)
            page = page_from(parse_document(text), row.path)
            result = self._store(page, disk, text.encode())
        await self._emit(id, row.path, "updated")
        return result

    async def move(self, id: PageId, folder: PurePosixPath, title: str | None = None) -> MoveResult:
        """Move and/or rename. A rename updates inbound ``[[id|Title]]`` link text."""
        current = await self.read(id)
        new_title = title if title is not None else current.page.title
        moved = await self.write(
            PageDraft(id, new_title, folder, current.page.body, current.page.jesinia),
            expected_rev=current.rev,
        )
        relinked: list[PageId] = []
        skipped: list[PageId] = []
        if new_title != current.page.title:
            for src in self._index.links_to(id):
                if src == id:
                    continue
                row = self._index.get(src)
                if row is None:
                    continue
                try:
                    linker = await self.read(src)
                    if linker.rev != row.rev:
                        raise StaleWrite(src)
                    body = retitle_links(linker.page.body, id, new_title)
                    if body != linker.page.body:
                        await self.update_body(src, body, expected_rev=row.rev)
                        relinked.append(src)
                except (StaleWrite, PageNotFound):
                    skipped.append(src)
        return MoveResult(moved, relinked, skipped)
