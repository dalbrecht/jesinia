# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Zotero, a real search index and a real sync service wired together."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import pytest

from jesinia.apps.zotero.application.sync import SyncService
from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.infrastructure.local_api import LocalApiClient, parse_paper
from jesinia.apps.zotero.infrastructure.mirror_store import SqliteMirrorStore
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.events import EventBus
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.infrastructure.fs import LocalFiles
from jesinia.services.kb.infrastructure.index import SqliteIndex
from jesinia.services.search.application.service import SearchService
from jesinia.services.search.infrastructure.fts import FtsIndex
from jesinia.testing.fake_zotero import FakeZotero

KB_DB = ModuleDb("kb", DbClass.DERIVED, files("jesinia.services.kb.migrations"))
SEARCH_DB = ModuleDb("search", DbClass.DERIVED, files("jesinia.services.search.migrations"))
MIRROR_DB = ModuleDb(
    "zotero_mirror", DbClass.DERIVED, files("jesinia.apps.zotero.migrations.mirror")
)


class NoKeyStore:
    async def read(self) -> str | None:
        return None

    async def write(self, key: str) -> None:
        return None


@dataclass
class Rig:
    fake: FakeZotero
    sync: SyncService
    search: SearchService
    mirror: SqliteMirrorStore
    library: LocalApiClient
    new_calls: list[ItemKey] = field(default_factory=list)
    gone_calls: list[ItemKey] = field(default_factory=list)
    server_changed_calls: list[str] = field(default_factory=list)
    moved_calls: list[tuple[ItemKey, ItemKey]] = field(default_factory=list)

    def add_paper(
        self,
        title: str,
        *,
        abstract: str = "",
        note: str | None = None,
        highlight: tuple[str, str] | None = None,
        fulltext: str | None = None,
    ) -> tuple[ItemKey, ItemKey]:
        """A paper with a PDF (and optional note, annotation and full text); (paper, pdf)."""
        key = self.fake.add_item(title, abstractNote=abstract)
        pdf = self.fake.add_item(
            "paper.pdf", item_type="attachment", parent=key, contentType="application/pdf"
        )
        if note is not None:
            self.fake.add_item("", item_type="note", parent=key, note=note)
        if highlight is not None:
            self.fake.add_item(
                "",
                item_type="annotation",
                parent=pdf,
                annotationText=highlight[0],
                annotationComment=highlight[1],
            )
        if fulltext is not None:
            self.fake.set_fulltext(pdf, fulltext)
        return key, pdf


@pytest.fixture
def fake() -> FakeZotero:
    return FakeZotero()


@pytest.fixture
async def rig(tmp_path: Path, fake: FakeZotero) -> AsyncIterator[Rig]:
    root = tmp_path / "kb"
    root.mkdir()
    kb_conn = open_db(tmp_path / "data", KB_DB)
    search_conn = open_db(tmp_path / "data", SEARCH_DB)
    mirror_conn = open_db(tmp_path / "data", MIRROR_DB)
    search = SearchService(
        FtsIndex(search_conn), KbService(LocalFiles(root), SqliteIndex(kb_conn), EventBus())
    )
    library = LocalApiClient("http://127.0.0.1:23119", NoKeyStore(), transport=fake.transport())
    mirror = SqliteMirrorStore(mirror_conn)
    holder: dict[str, Rig] = {}

    async def on_new(key: ItemKey) -> None:
        holder["rig"].new_calls.append(key)

    async def on_gone(key: ItemKey) -> None:
        holder["rig"].gone_calls.append(key)

    async def on_server_changed(old: str) -> None:
        holder["rig"].server_changed_calls.append(old)

    async def on_moved(old: ItemKey, parent: ItemKey) -> None:
        holder["rig"].moved_calls.append((old, parent))

    sync = SyncService(
        library,
        mirror,
        search,
        on_new,
        on_gone,
        on_server_changed,
        on_moved=on_moved,
        parse=parse_paper,
    )
    holder["rig"] = Rig(fake, sync, search, mirror, library)
    yield holder["rig"]
    for conn in (kb_conn, search_conn, mirror_conn):
        conn.close()
