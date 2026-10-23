# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SQLite mirror round-trips papers and keeps its cursor."""

from __future__ import annotations

from pathlib import Path

import pytest

from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.domain.paper import Attachment
from jesinia.apps.zotero.infrastructure.mirror_store import SqliteMirrorStore
from jesinia.core.db import open_db
from tests.apps.zotero.application.test_sync import make_paper
from tests.apps.zotero.conftest import MIRROR_DB


def test_should_round_trip_a_paper_and_its_fulltext(tmp_path: Path) -> None:
    store = SqliteMirrorStore(open_db(tmp_path, MIRROR_DB))
    paper = make_paper()
    assert store.get(paper.key) is None and store.fulltext(paper.key) is None
    assert store.fulltext_version(ItemKey("A1")) == 0
    store.put(paper, "words", {ItemKey("A1"): 7})
    assert store.get(paper.key) == paper
    assert store.fulltext(paper.key) == "words" and store.fulltext_version(ItemKey("A1")) == 7
    store.put(paper, None, {ItemKey("A1"): 9})
    assert store.fulltext(paper.key) is None
    assert store.fulltext_version(ItemKey("A1")) == 9
    assert store.all_keys() == [paper.key]


def test_should_find_the_paper_owning_an_attachment(tmp_path: Path) -> None:
    store = SqliteMirrorStore(open_db(tmp_path, MIRROR_DB))
    store.put(make_paper(), None)
    assert store.owner_of(ItemKey("A1")) == "K1" and store.owner_of(ItemKey("K1")) is None
    assert store.owner_of(ItemKey("nope")) is None


def test_should_remove_and_clear_and_keep_meta(tmp_path: Path) -> None:
    store = SqliteMirrorStore(open_db(tmp_path, MIRROR_DB))
    assert store.library_version() == 0 and store.server_id() is None
    store.set_library_version(9)
    store.set_server_id("srv")
    store.put(make_paper(), "t", {ItemKey("A1"): 1})
    store.put(make_paper(key=ItemKey("K2"), attachments=()), None)
    store.remove(ItemKey("K1"))
    assert store.all_keys() == ["K2"] and store.fulltext(ItemKey("K1")) is None
    store.clear()
    assert store.all_keys() == [] and store.library_version() == 0 and store.server_id() == "srv"


def test_should_roll_back_a_failed_write(tmp_path: Path) -> None:
    store = SqliteMirrorStore(open_db(tmp_path, MIRROR_DB))
    with pytest.raises(OverflowError):
        store.put(make_paper(version=2**70), "text", {ItemKey("A1"): 1})
    assert store.all_keys() == []
    store.put(make_paper(), None)
    assert store.all_keys() == ["K1"]


def test_should_map_every_child_to_its_paper_and_skip_standalone_self_row(tmp_path: Path) -> None:
    store = SqliteMirrorStore(open_db(tmp_path, MIRROR_DB))
    store.put(make_paper(), None)
    standalone = make_paper(
        key=ItemKey("S1"),
        notes=(),
        standalone=True,
        attachments=(Attachment(ItemKey("S1"), "s", "application/pdf", "s.pdf", "", ()),),
    )
    store.put(standalone, None)
    assert store.descendant_keys() == {
        ItemKey("N1"): ItemKey("K1"),
        ItemKey("A1"): ItemKey("K1"),
        ItemKey("X1"): ItemKey("K1"),
    }
    store.put(make_paper(notes=()), None)
    assert ItemKey("N1") not in store.descendant_keys()
    store.remove(ItemKey("K1"))
    assert store.descendant_keys() == {}
    store.put(make_paper(), None)
    store.clear()
    assert store.descendant_keys() == {}
