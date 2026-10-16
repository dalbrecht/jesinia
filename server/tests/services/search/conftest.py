# SPDX-License-Identifier: AGPL-3.0-or-later
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import pytest

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.events import EventBus
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.infrastructure.fs import LocalFiles
from jesinia.services.kb.infrastructure.index import SqliteIndex
from jesinia.services.search.application.service import SearchService
from jesinia.services.search.infrastructure.fts import FtsIndex

KB_DB = ModuleDb("kb", DbClass.DERIVED, files("jesinia.services.kb.migrations"))
SEARCH_DB = ModuleDb("search", DbClass.DERIVED, files("jesinia.services.search.migrations"))


@dataclass
class Env:
    kb: KbService
    search: SearchService
    bus: EventBus
    root: Path


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "kb"
    root.mkdir()
    bus = EventBus()
    kconn = open_db(tmp_path / "data", KB_DB)
    sconn = open_db(tmp_path / "data", SEARCH_DB)
    kb = KbService(LocalFiles(root), SqliteIndex(kconn), bus)
    search = SearchService(FtsIndex(sconn), kb)
    search.subscribe(bus)
    yield Env(kb, search, bus, root)
    kconn.close()
    sconn.close()
