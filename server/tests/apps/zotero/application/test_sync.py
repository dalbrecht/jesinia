# SPDX-License-Identifier: AGPL-3.0-or-later
"""Syncing the Zotero library into the mirror and the search index."""

from __future__ import annotations

import pytest

from jesinia.apps.zotero.application.sync import (
    SEARCH_BODY_LIMIT,
    SyncReport,
    SyncService,
    html_to_text,
    search_record,
)
from jesinia.apps.zotero.domain.errors import ZoteroUnavailable
from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.domain.paper import (
    Annotation,
    Attachment,
    Creator,
    Note,
    Paper,
    ZoteroTag,
)
from jesinia.apps.zotero.infrastructure.local_api import parse_paper
from tests.apps.zotero.conftest import Rig


def make_paper(**overrides: object) -> Paper:
    fields: dict[str, object] = {
        "key": ItemKey("K1"),
        "version": 1,
        "item_type": "journalArticle",
        "title": "Diffusion Policy",
        "creators": (Creator("Cheng", "Chi", "author"), Creator("Shuran", "Song", "author")),
        "date": "2023-03-07",
        "abstract": "visuomotor policy learning",
        "doi": "",
        "url": "",
        "extra": "",
        "tags": (ZoteroTag("robotics", False),),
        "related": (),
        "notes": (Note(ItemKey("N1"), "<p>my <b>note</b> &amp; thoughts</p>"),),
        "attachments": (
            Attachment(
                ItemKey("A1"),
                "p",
                "application/pdf",
                "p.pdf",
                "",
                (Annotation(ItemKey("X1"), "highlighted", "my comment", "3", "#ff0"),),
            ),
        ),
        "standalone": False,
    }
    fields.update(overrides)
    return Paper(**fields)  # type: ignore[arg-type]


async def hit_keys(rig: Rig, query: str) -> list[str]:
    return [hit.key for hit in await rig.search.query(query, sources={"zotero"})]


async def test_should_index_new_paper_with_fulltext_and_annotations(rig: Rig) -> None:
    key, _ = rig.add_paper(
        "Diffusion Policy",
        abstract="about actions",
        highlight=("visuomotor", "key idea"),
        fulltext="robot actions",
    )
    report = await rig.sync.sync()
    hits = await rig.search.query("visuomotor", sources={"zotero"})
    assert hits[0].key == key and hits[0].url == f"zotero://select/library/items/{key}"
    assert await hit_keys(rig, "robot") == [key] and await hit_keys(rig, "idea") == [key]
    assert report == SyncReport(changed=1, removed=0, fulltext_updated=0)
    assert rig.new_calls == [key]


async def test_should_index_standalone_pdf_under_its_attachment_key(rig: Rig) -> None:
    pdf = rig.fake.add_item("Loose Scan", item_type="attachment", contentType="application/pdf")
    rig.fake.add_item("", item_type="annotation", parent=pdf, annotationText="loosehighlight")
    await rig.sync.sync()
    assert await hit_keys(rig, "loosehighlight") == [pdf]


async def test_should_skip_top_level_notes_and_non_pdf_attachments(rig: Rig) -> None:
    rig.fake.add_item("", item_type="note", note="<p>lonely</p>")
    rig.fake.add_item("page", item_type="attachment", contentType="text/html")
    report = await rig.sync.sync()
    assert report.changed == 0 and rig.mirror.all_keys() == []


async def test_should_pick_up_fulltext_that_arrives_later(rig: Rig) -> None:
    key, pdf = rig.add_paper("Slow Bloomer")
    await rig.sync.sync()
    assert await hit_keys(rig, "late") == []
    rig.fake.set_fulltext(pdf, "late words")
    rig.fake.add_item("extra", item_type="note", parent=key, note="touch")
    await rig.sync.sync()
    assert await hit_keys(rig, "late") == [key]


async def test_should_pick_up_fulltext_via_fulltext_feed_without_item_change(rig: Rig) -> None:
    key, pdf = rig.add_paper("Quiet Change")
    await rig.sync.sync()
    version_before = rig.mirror.library_version()
    rig.fake.set_fulltext(pdf, "late words")
    report = await rig.sync.sync()
    assert await hit_keys(rig, "late") == [key]
    assert rig.mirror.fulltext(key) == "late words"
    assert report == SyncReport(changed=0, removed=0, fulltext_updated=1)
    assert rig.mirror.library_version() == version_before
    again = await rig.sync.sync()
    assert again.fulltext_updated == 0


async def test_should_join_fulltext_of_every_pdf_of_a_paper(rig: Rig) -> None:
    key, pdf = rig.add_paper("Two Files", fulltext="first text")
    second = rig.fake.add_item(
        "b.pdf", item_type="attachment", parent=key, contentType="application/pdf"
    )
    rig.fake.set_fulltext(second, "second text")
    await rig.sync.sync()
    assert await hit_keys(rig, "first") == [key] and await hit_keys(rig, "second") == [key]


async def test_should_resync_parent_when_a_child_note_changes(rig: Rig) -> None:
    key, _ = rig.add_paper("Parent Paper")
    await rig.sync.sync()
    rig.fake.add_item("", item_type="note", parent=key, note="<p>freshnote</p>")
    report = await rig.sync.sync()
    assert await hit_keys(rig, "freshnote") == [key]
    assert report.changed == 1 and rig.new_calls == [key]


async def test_should_resync_parent_when_an_annotation_changes(rig: Rig) -> None:
    key, pdf = rig.add_paper("Annotated")
    await rig.sync.sync()
    rig.fake.add_item("", item_type="annotation", parent=pdf, annotationText="newmark")
    await rig.sync.sync()
    assert await hit_keys(rig, "newmark") == [key]


async def test_should_resync_parent_when_a_child_is_trashed(rig: Rig) -> None:
    key = rig.fake.add_item("Holder")
    note = rig.fake.add_item("", item_type="note", parent=key, note="<p>doomednote</p>")
    await rig.sync.sync()
    assert await hit_keys(rig, "doomednote") == [key]
    rig.fake.trash(note)
    await rig.sync.sync()
    assert await hit_keys(rig, "doomednote") == []
    assert rig.gone_calls == []


async def test_should_ignore_children_of_a_trashed_paper(rig: Rig) -> None:
    key = rig.fake.add_item("Holder")
    await rig.sync.sync()
    rig.fake.add_item("", item_type="note", parent=key, note="<p>x</p>")
    rig.fake.trash(key)
    await rig.sync.sync()
    assert rig.mirror.all_keys() == [] and rig.gone_calls == [key]


async def test_should_not_advance_cursor_when_a_feed_fails(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add_paper("First")
    await rig.sync.sync()
    cursor = rig.mirror.library_version()
    rig.add_paper("Second")

    async def broken(since: int) -> dict[ItemKey, int]:
        raise ZoteroUnavailable("not_running")

    monkeypatch.setattr(rig.library, "fulltext_versions", broken)
    with pytest.raises(ZoteroUnavailable):
        await rig.sync.sync()
    assert rig.mirror.library_version() == cursor
    monkeypatch.undo()
    report = await rig.sync.sync()
    assert report.changed == 1 and rig.mirror.library_version() > cursor


async def test_should_rebuild_mirror_when_server_id_changes(rig: Rig) -> None:
    key = rig.fake.add_item("Old Library Paper")
    await rig.sync.sync()
    assert rig.server_changed_calls == []
    old_id = rig.fake.server_id
    rig.fake.server_id = "other"
    rig.fake.remove(key)
    fresh = rig.fake.add_item("New Library Paper")
    await rig.sync.sync()
    assert rig.server_changed_calls == [old_id]
    assert await hit_keys(rig, "old") == [] and await hit_keys(rig, "new") == [fresh]
    assert rig.mirror.all_keys() == [fresh]
    await rig.sync.sync()
    assert rig.server_changed_calls == [old_id]


async def test_should_unregister_paper_trashed_or_deleted_in_zotero(rig: Rig) -> None:
    keep = rig.fake.add_item("keeper paper")
    trashed = rig.fake.add_item("trashedword")
    deleted = rig.fake.add_item("deletedword")
    await rig.sync.sync()
    rig.fake.trash(trashed)
    rig.fake.remove(deleted)
    report = await rig.sync.sync()
    assert await hit_keys(rig, "trashedword") == [] and await hit_keys(rig, "deletedword") == []
    assert await hit_keys(rig, "keeper") == [keep]
    assert sorted(rig.gone_calls) == sorted([trashed, deleted])
    assert report.removed == 2 and rig.mirror.all_keys() == [keep]


async def test_should_reregister_all_papers_from_the_mirror(rig: Rig) -> None:
    key, _ = rig.add_paper("Remembered", fulltext="stored words")
    await rig.sync.sync()
    await rig.search.unregister("zotero", key)
    assert await hit_keys(rig, "remembered") == []
    await rig.sync.reregister_all()
    assert await hit_keys(rig, "remembered") == [key]
    assert await hit_keys(rig, "stored") == [key]


def test_should_cap_search_body_at_limit() -> None:
    rec = search_record(make_paper(), "w " * 300_000)
    assert len(rec.body) <= SEARCH_BODY_LIMIT
    assert rec.body.startswith("visuomotor policy learning")


def test_should_build_title_from_title_first_author_and_year() -> None:
    rec = search_record(make_paper(), None)
    assert rec.title == "Diffusion Policy — Chi 2023"
    assert rec.source == "zotero" and rec.key == "K1"
    assert rec.url == "zotero://select/library/items/K1"


def test_should_drop_missing_title_parts_cleanly() -> None:
    assert search_record(make_paper(date="n.d."), None).title == "Diffusion Policy — Chi"
    assert search_record(make_paper(creators=()), None).title == "Diffusion Policy — 2023"
    assert search_record(make_paper(creators=(), date=""), None).title == "Diffusion Policy"
    assert search_record(make_paper(title=""), None).title == "Chi 2023"
    unnamed = Creator("", "", "author")
    assert search_record(make_paper(creators=(unnamed,)), None).title.endswith("— 2023")


def test_should_order_body_abstract_tags_notes_annotations_fulltext() -> None:
    body = search_record(make_paper(), "full text here").body
    order = ["visuomotor", "robotics", "my note & thoughts", "highlighted", "my comment", "full"]
    positions = [body.index(part) for part in order]
    assert positions == sorted(positions)


def test_should_strip_html_from_notes() -> None:
    assert html_to_text("<div><p>a<br/>b</p><script>x</script></div>").split() == ["a", "b"]
    assert html_to_text("1 &lt; 2") == "1 < 2"


def test_should_use_the_attachment_key_for_a_standalone_pdf() -> None:
    paper = make_paper(key=ItemKey("A1"), standalone=True, notes=())
    assert search_record(paper, None).key == "A1"


async def test_should_report_nothing_when_library_is_empty(rig: Rig) -> None:
    assert await rig.sync.sync() == SyncReport(0, 0, 0)


async def test_should_work_without_any_hooks(rig: Rig) -> None:
    bare = SyncService(rig.library, rig.mirror, rig.search, parse=parse_paper)
    key = rig.fake.add_item("Hookless")
    await bare.sync()
    rig.fake.server_id = "swapped"
    rig.fake.trash(key)
    await bare.sync()
    assert rig.mirror.all_keys() == []
