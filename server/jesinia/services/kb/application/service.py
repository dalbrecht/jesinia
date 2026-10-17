# SPDX-License-Identifier: AGPL-3.0-or-later
"""``KbService``: read, write, update, move, and browse pages (spec §4.1)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from jesinia.core.events import Event, EventBus
from jesinia.services.kb.application.errors import (
    AssetNotFound,
    InvalidAssetName,
    KbError,
    KbUnavailable,
    PageNotFound,
    PathOutsideKb,
    ReservedPath,
    StaleWrite,
    UnsupportedAssetType,
)
from jesinia.services.kb.application.ports import Conflict, PageFiles, PageIndex
from jesinia.services.kb.domain.assets import image_media_type, is_asset_file_name, is_page_id
from jesinia.services.kb.domain.frontmatter import (
    page_from,
    parse_document,
    replace_body,
    serialize_page,
)
from jesinia.services.kb.domain.links import link_targets, retitle_links
from jesinia.services.kb.domain.page import Page, PageId, new_page_id, source_of
from jesinia.services.kb.domain.paths import (
    filename_for,
    is_hidden,
    is_page_candidate,
    is_reserved,
    sanitize_title,
)
from jesinia.services.kb.domain.tag_catalog import indexed_tags

log = logging.getLogger(__name__)

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


NEW_ID_PLACEHOLDER = "new page id"
NO_KB_PATH = "no KB path is configured"
ASSETS_FOLDER = "_assets"
# What an asset folder's .gitignore holds when its files must stay on this machine.
LOCAL_ONLY_GITIGNORE = b"*\n"


@dataclass(frozen=True)
class Asset:
    """A page asset's bytes and the image type they were sniffed as."""

    data: bytes
    media_type: str


class KbService:
    def __init__(self, files: PageFiles | None, index: PageIndex, bus: EventBus) -> None:
        self._files = files
        self._problem: str | None = None
        self._index = index
        self._bus = bus
        self._locks: defaultdict[PageId, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- helpers -------------------------------------------------------------------
    @property
    def root(self) -> Path | None:
        return self._files.root if self._files is not None else None

    def _fs(self) -> PageFiles:
        if self._files is None:
            raise KbUnavailable(self._problem or NO_KB_PATH)
        return self._files

    def require_available(self) -> None:
        """Raise ``KbUnavailable`` with the reason (the KB banner's message) when no KB can
        be written; callers check this before acting so nothing is half-done or queued."""
        self._fs()

    @property
    def files(self) -> PageFiles | None:
        return self._files

    @property
    def index(self) -> PageIndex:
        return self._index

    def set_files(self, files: PageFiles | None, problem: str | None = None) -> None:
        """Point the service at a new KB root (``PUT /api/kb/path``); with no files,
        ``problem`` says why (it becomes the ``KbUnavailable`` message)."""
        self._files = files
        self._problem = problem if files is None else None

    async def conflicts(self) -> list[Conflict]:
        return self._index.conflicts()

    async def emit(self, id: PageId, path: PurePosixPath, change: Change, origin: str) -> None:
        payload = {"id": id, "path": str(path), "change": change, "origin": origin}
        await self._bus.publish(Event("kb.page_changed", payload))

    def resolve(self, id: PageId) -> PurePosixPath | None:
        row = self._index.get(id)
        return row.path if row else None

    def _check_folder(self, folder: PurePosixPath) -> PurePosixPath:
        if folder.is_absolute() or ".." in folder.parts:
            raise PathOutsideKb(f"{folder}: must be a relative path inside the KB")
        probe = folder / "x.md"
        if is_reserved(probe) or not is_page_candidate(probe):
            raise ReservedPath(
                f"{folder}: Jesinia never writes pages in reserved, hidden or _assets folders"
            )
        self._fs().contain(folder)
        return nfc(folder)

    def planned_path(
        self, folder: PurePosixPath, title: str, id: PageId | None = None
    ) -> tuple[PurePosixPath, bool]:
        """Where a write would put the page, using the same resolution as the write.

        For a new page (``id`` None) the collision suffix depends on the id it gets at
        write time, so a suffixed path shows ``[new page id]`` rather than a sampled id,
        and the second value says whether a suffix will be added.
        """
        checked = self._check_folder(folder)
        path = self._target(checked, title, id or new_page_id())
        suffixed = path.name != f"{sanitize_title(title)}.md"
        if suffixed and id is None:
            path = path.with_name(f"{sanitize_title(title)} [{NEW_ID_PLACEHOLDER}].md")
        return path, suffixed

    def _target(self, folder: PurePosixPath, title: str, id: PageId) -> PurePosixPath:
        """A free path for the page: the title, else title + id prefix, else the full id."""
        siblings = [(r.title, r.id) for r in self._index.all() if r.path.parent == folder]
        name, hex_id = sanitize_title(title), id.replace("-", "")
        candidates = [
            filename_for(title, id, siblings),
            f"{name} [{hex_id[:8]}].md",
            f"{name} [{hex_id}].md",
            *(f"{name} [{hex_id}] {n}.md" for n in range(2, 100)),
        ]
        fs = self._fs()
        for candidate in candidates:
            path = folder / candidate
            existing = self._index.by_path(path)
            taken = existing.id != id if existing else fs.exists(str(path))
            if not taken:
                fs.contain(path)
                return path
        raise StaleWrite(f"no free file name for {title!r} in {folder}")  # pragma: no cover

    async def _emit(self, id: PageId, path: PurePosixPath, change: Change) -> None:
        await self.emit(id, path, change, "jesinia")

    def _index_put(
        self, page: Page, path: PurePosixPath, disk: str, rev: str, mtime: float
    ) -> None:
        # The file is the source of truth and is already written. The index is derived:
        # if updating it fails, report success anyway. Row and links change in one
        # transaction, so a failure leaves the old mtime and the next scan re-reads the file.
        try:
            self._index.put(
                page.id,
                path,
                disk,
                rev,
                page.title,
                mtime,
                link_targets(page.body),
                indexed_tags(page.body, page.jesinia),
                source_of(page.jesinia),
            )
        except Exception:
            log.exception("KB index update failed for %s; the next scan will repair it", page.id)

    def _store(
        self,
        page: Page,
        disk: str,
        data: bytes,
        *,
        expected_rev: str | None,
        old_path: PurePosixPath | None = None,
    ) -> PageWithRev:
        """Write ``data`` at ``disk``, then (when ``page.path`` differs from ``old_path``)
        move it there. ``expected_rev=None`` creates a new file and never replaces one.

        Content is committed before the path changes, and both steps are atomic: if the
        move fails, the page stays readable at its old path with the new content.
        """
        fs = self._fs()
        mtime = fs.write_atomic(disk, data, expected_rev=expected_rev, create=expected_rev is None)
        rev = sha256(data)
        if old_path is not None and page.path != old_path:
            try:
                fs.rename(disk, str(page.path))
            except BaseException:
                self._index_put(page, old_path, disk, rev, mtime)
                raise
            disk = str(page.path)
        self._index_put(page, page.path, disk, rev, mtime)
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
                result = self._store(
                    page, str(path), serialize_page(page).encode(), expected_rev=None
                )
            await self._emit(id, path, "created")
            return result

        id = draft.id
        async with self._locks[id]:
            disk, data = self._current(id)
            if expected_rev is None or sha256(data) != expected_rev:
                raise StaleWrite(f"{id}: changed since read (or no base revision given)")
            base_rev = expected_rev
            row = self._index.get(id)
            assert row is not None
            current = page_from(parse_document(data.decode("utf-8")), row.path)
            moved = draft.title != current.title or folder != row.path.parent
            path = self._target(folder, draft.title, id) if moved else row.path
            page = Page(id, draft.title, path, draft.body, dict(draft.jesinia), current.foreign)
            result = self._store(
                page, disk, serialize_page(page).encode(), expected_rev=base_rev,
                old_path=row.path,
            )  # fmt: skip
        await self._emit(id, path, "moved" if path != row.path else "updated")
        return result

    async def write_asset(
        self, id: PageId, name: str, data: bytes, *, local_only: bool = False
    ) -> PurePosixPath:
        """Store a file for page ``id`` at ``_assets/<id>/<name>`` (replacing one of that
        name) and return that KB-relative path. ``local_only`` gives the folder a
        ``.gitignore`` of ``*``, so KB sync never commits its files.

        Raises ``PageNotFound`` (the id must be an indexed page, which also keeps it a plain
        path segment), ``InvalidAssetName`` or ``KbUnavailable``.
        """
        fs = self._fs()
        if self._index.get(id) is None:
            raise PageNotFound(id)
        if not is_asset_file_name(name):
            raise InvalidAssetName(f"{name!r} is not a plain asset file name")
        folder = PurePosixPath(ASSETS_FOLDER, id)
        if local_only:
            ignore = folder / ".gitignore"
            fs.contain(ignore)
            if not fs.exists(str(ignore)):
                fs.write_atomic(str(ignore), LOCAL_ONLY_GITIGNORE)
        path = folder / name
        fs.contain(path)
        # Up to megabytes with an fsync: keep it off the event loop.
        await asyncio.to_thread(fs.write_atomic, str(path), data)
        return path

    async def read_asset(self, page_id: str, name: str) -> Asset:
        """The image stored at ``_assets/<page_id>/<name>``, typed by its bytes.

        Raises ``InvalidAssetName`` (``page_id`` is not a page id, or ``name`` is not one
        plain file name), ``PathOutsideKb`` (a symlink on the way), ``AssetNotFound``,
        ``UnsupportedAssetType`` (not PNG, JPEG, GIF, WebP or AVIF) or ``KbUnavailable``.
        """
        fs = self._fs()
        if not is_page_id(page_id):
            raise InvalidAssetName(f"{page_id!r} is not a page id")
        if not is_asset_file_name(name):
            raise InvalidAssetName(f"{name!r} is not a plain asset file name")
        disk = f"{ASSETS_FOLDER}/{page_id}/{name}"
        # Resolved, the file must sit directly in this page's folder: never another page's
        # (a linked folder), never anywhere else.
        folder = fs.root.resolve() / ASSETS_FOLDER / page_id
        if fs.contain(PurePosixPath(disk)).parent != folder:
            raise PathOutsideKb(f"{disk} is not in {ASSETS_FOLDER}/{page_id}/")
        try:
            data = await asyncio.to_thread(fs.read_regular, disk)
        except FileNotFoundError as exc:
            raise AssetNotFound(disk) from exc
        media_type = image_media_type(data)
        if media_type is None:
            raise UnsupportedAssetType(f"{disk} is not a PNG, JPEG, GIF, WebP or AVIF image")
        return Asset(data, media_type)

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
            result = self._store(page, disk, text.encode(), expected_rev=expected_rev)
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
                except (KbError, OSError, ValueError):
                    # Stale, missing, unreadable, or malformed linkers are left alone; the
                    # rename itself already succeeded.
                    skipped.append(src)
        return MoveResult(moved, relinked, skipped)
