# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
from pathlib import PurePosixPath

import pytest

from jesinia.core.events import Event
from jesinia.services.kb.application.errors import (
    KbUnavailable,
    PageNotFound,
    PathOutsideKb,
    ReservedPath,
    StaleWrite,
)
from jesinia.services.kb.application.service import KbService, PageDraft
from jesinia.services.kb.domain.page import PageId

N = PurePosixPath("Notes")


async def test_write_creates_file_in_corpus_format(kb: KbService, events: list[Event]) -> None:
    p = await kb.write(PageDraft(None, "Hello", N, "Body", {"kind": "note"}))
    text = (kb.root / "Notes/Hello.md").read_text()  # type: ignore[operator]
    assert text.startswith(f"---\nid: {p.page.id}\ntitle: Hello\n")
    assert text.endswith("jesinia:\n  kind: note\n---\nBody")
    assert p.rev == hashlib.sha256(text.encode()).hexdigest()
    assert kb.resolve(p.page.id) == PurePosixPath("Notes/Hello.md")
    assert events[-1].payload == {
        "id": p.page.id,
        "path": "Notes/Hello.md",
        "change": "created",
        "origin": "jesinia",
    }


async def test_read_round_trip(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "R", N, "body text"))
    r = await kb.read(p.page.id)
    assert r.page.body == "body text" and r.page.title == "R" and r.rev == p.rev


async def test_stale_write_refused_after_external_edit(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "A", N, "v1"))
    f = kb.root / "Notes/A.md"  # type: ignore[operator]
    f.write_text(f.read_text().replace("v1", "user"))
    with pytest.raises(StaleWrite):
        await kb.write(PageDraft(p.page.id, "A", N, "agent"), expected_rev=p.rev)
    assert "user" in f.read_text()


async def test_update_without_rev_is_refused(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "A", N, "v1"))
    with pytest.raises(StaleWrite):
        await kb.write(PageDraft(p.page.id, "A", N, "v2"))


@pytest.mark.parametrize("folder", ["../x", "Notes/../../x", "/etc"])
async def test_path_escape_rejected(kb: KbService, folder: str) -> None:
    with pytest.raises(PathOutsideKb):
        await kb.write(PageDraft(None, "x", PurePosixPath(folder), ""))


async def test_symlink_escape_rejected(kb: KbService, tmp_path) -> None:  # type: ignore[no-untyped-def]
    outside = tmp_path / "outside"
    outside.mkdir()
    (kb.root / "link").symlink_to(outside)  # type: ignore[operator]
    with pytest.raises(PathOutsideKb):
        await kb.write(PageDraft(None, "x", PurePosixPath("link"), ""))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("folder", ["Journal", "drafts/sub", ".hidden"])
async def test_reserved_and_hidden_rejected(kb: KbService, folder: str) -> None:
    with pytest.raises(ReservedPath):
        await kb.write(PageDraft(None, "x", PurePosixPath(folder), ""))


async def test_title_change_moves_file_and_keeps_id(kb: KbService, events: list[Event]) -> None:
    p = await kb.write(PageDraft(None, "Old", N, "b"))
    q = await kb.write(PageDraft(p.page.id, "New", N, "b"), expected_rev=p.rev)
    root = kb.root
    assert root is not None
    assert not (root / "Notes/Old.md").exists() and (root / "Notes/New.md").exists()
    assert q.page.id == p.page.id and events[-1].payload["change"] == "moved"


async def test_write_preserves_foreign_keys(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "F", N, "b"))
    f = kb.root / "Notes/F.md"  # type: ignore[operator]
    f.write_text(f.read_text().replace("title: F\n", "title: F\naliases: [ff]\n"))
    cur = await kb.read(p.page.id)
    await kb.write(PageDraft(p.page.id, "F", N, "b2"), expected_rev=cur.rev)
    assert "aliases:\n- ff\n" in f.read_text()


async def test_same_title_gets_id_suffix(kb: KbService) -> None:
    a = await kb.write(PageDraft(None, "Same", N, "a"))
    b = await kb.write(PageDraft(None, "same", N, "b"))
    paths = {kb.resolve(a.page.id), kb.resolve(b.page.id)}
    assert PurePosixPath("Notes/Same.md") in paths
    assert len(paths) == 2


async def test_new_page_never_overwrites_unindexed_file(kb: KbService) -> None:
    root = kb.root
    assert root is not None
    (root / "Notes").mkdir()
    (root / "Notes/Taken.md").write_text("someone else's file\n")
    p = await kb.write(PageDraft(None, "Taken", N, "mine"))
    assert (root / "Notes/Taken.md").read_text() == "someone else's file\n"
    assert kb.resolve(p.page.id) != PurePosixPath("Notes/Taken.md")


async def test_update_body_preserves_noncanonical_frontmatter(kb: KbService) -> None:
    made = await kb.write(PageDraft(None, "Odd", N, "v1\n"))
    raw = f"---\nid: {made.page.id}\ntitle: 'Odd'  # keep me\naliases: [rt]\n---\n"
    f = kb.root / "Notes/Odd.md"  # type: ignore[operator]
    f.write_text(raw + "v1\n")  # user reformats the YAML in another editor
    p = await kb.read(made.page.id)  # rev = hash of the current bytes
    await kb.update_body(p.page.id, "v2\n", expected_rev=p.rev)
    assert f.read_text() == raw + "v2\n"


async def test_update_body_stale_refused(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "S", N, "v1"))
    f = kb.root / "Notes/S.md"  # type: ignore[operator]
    f.write_text(f.read_text().replace("v1", "user"))
    with pytest.raises(StaleWrite):
        await kb.update_body(p.page.id, "mine", expected_rev=p.rev)


async def test_rename_retitles_inbound_links(kb: KbService) -> None:
    a = await kb.write(PageDraft(None, "Target", N, "t"))
    b = await kb.write(PageDraft(None, "Linker", N, f"see [[{a.page.id}|Target]]"))
    r = await kb.move(a.page.id, N, title="Renamed")
    linker = await kb.read(b.page.id)
    assert f"[[{a.page.id}|Renamed]]" in linker.page.body and r.relinked == [b.page.id]
    assert r.page.page.title == "Renamed" and r.page.page.path == PurePosixPath("Notes/Renamed.md")


async def test_move_to_folder_needs_no_relink(kb: KbService) -> None:
    a = await kb.write(PageDraft(None, "T", N, "t"))
    await kb.write(PageDraft(None, "L", N, f"[[{a.page.id}|T]]"))
    r = await kb.move(a.page.id, PurePosixPath("Archive"))
    assert r.relinked == [] and r.page.page.path == PurePosixPath("Archive/T.md")


async def test_relink_skips_stale_linker(kb: KbService) -> None:
    a = await kb.write(PageDraft(None, "T", N, "t"))
    b = await kb.write(PageDraft(None, "L", N, f"[[{a.page.id}|T]]"))
    kb._index.put(  # simulate an index that is behind the file (external edit not yet seen)
        b.page.id, PurePosixPath("Notes/L.md"), "Notes/L.md", "stale-rev", "L", 0.0
    )
    r = await kb.move(a.page.id, N, title="T2")
    assert r.relinked == [] and r.skipped == [b.page.id]


async def test_tree_counts_and_order(kb: KbService) -> None:
    await kb.write(PageDraft(None, "B", N, ""))
    await kb.write(PageDraft(None, "A", PurePosixPath("Notes/Sub"), ""))
    await kb.write(PageDraft(None, "Top", PurePosixPath(""), ""))
    tree = await kb.tree()
    assert [n.kind for n in tree] == ["folder", "page"]
    notes = next(n for n in tree if n.path == N)
    assert notes.pages == 2 and [c.kind for c in notes.children] == ["folder", "page"]
    assert notes.children[1].title == "B" and notes.children[1].id is not None


async def test_read_missing_page(kb: KbService) -> None:
    with pytest.raises(PageNotFound):
        await kb.read(PageId("0192f7a8-0000-7000-8000-0000000000ff"))
    p = await kb.write(PageDraft(None, "Gone", N, ""))
    (kb.root / "Notes/Gone.md").unlink()  # type: ignore[operator]
    with pytest.raises(PageNotFound):
        await kb.read(p.page.id)


async def test_unavailable_when_no_kb_path(kb_unset: KbService) -> None:
    with pytest.raises(KbUnavailable):
        await kb_unset.read(PageId("x"))
    with pytest.raises(KbUnavailable):
        await kb_unset.write(PageDraft(None, "x", N, ""))
    assert kb_unset.root is None
