# SPDX-License-Identifier: AGPL-3.0-or-later
from importlib.resources import files
from pathlib import Path

import pytest

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.events import Event, EventBus
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.infrastructure.fs import LocalFiles
from jesinia.services.kb.infrastructure.index import SqliteIndex

KB_DB = ModuleDb("kb", DbClass.DERIVED, files("jesinia.services.kb.migrations"))


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def bus(events: list[Event]) -> EventBus:
    b = EventBus()

    async def record(e: Event) -> None:
        events.append(e)

    b.subscribe("kb.page_changed", record)
    return b


@pytest.fixture
def kb(tmp_path: Path, bus: EventBus):  # type: ignore[no-untyped-def]
    root = tmp_path / "kb"
    root.mkdir()
    conn = open_db(tmp_path / "data", KB_DB)
    svc = KbService(LocalFiles(root), SqliteIndex(conn), bus)
    yield svc
    conn.close()


@pytest.fixture
def kb_unset(tmp_path: Path, bus: EventBus):  # type: ignore[no-untyped-def]
    conn = open_db(tmp_path / "data", KB_DB)
    yield KbService(None, SqliteIndex(conn), bus)
    conn.close()
