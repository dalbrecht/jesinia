# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
from pathlib import PurePosixPath

import pytest

from jesinia.core.events import Event
from jesinia.services.kb.application.errors import (
    AgentOnlyPage,
    InvalidAssetName,
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


@pytest.mark.parametrize("folder", ["Journal", "drafts/sub", ".hidden", "_assets/x", "a/_assets"])
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


async def test_index_failure_after_write_still_succeeds(
    kb: KbService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import sqlite3

    def locked(*a: object, **k: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.index, "put", locked)
    p = await kb.write(PageDraft(None, "Durable", N, "body"))
    assert (kb.root / "Notes/Durable.md").read_text().endswith("body")  # type: ignore[operator]
    assert p.page.title == "Durable" and "next scan will repair" in caplog.text


async def test_relink_skips_malformed_linker(kb: KbService) -> None:
    a = await kb.write(PageDraft(None, "T", N, "t"))
    b = await kb.write(PageDraft(None, "L", N, f"[[{a.page.id}|T]]"))
    (kb.root / "Notes/L.md").write_bytes(b"---\nid: " + b.page.id.encode() + b"\n---\n\xff\xfe")  # type: ignore[operator]
    r = await kb.move(a.page.id, N, title="T2")
    assert r.page.page.title == "T2" and r.skipped == [b.page.id] and r.relinked == []


async def test_failed_link_update_leaves_row_for_scan_to_repair(
    kb: KbService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from jesinia.services.kb.application.scan import scan
    from jesinia.services.kb.infrastructure.index import SqliteIndex

    target = await kb.write(PageDraft(None, "Target", N, ""))
    src = await kb.write(PageDraft(None, "Src", N, "no links yet"))

    def broken(self: SqliteIndex, *a: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SqliteIndex, "_replace_links", broken)
    body = f"see [[{target.page.id}|Target]]"
    await kb.update_body(src.page.id, body, expected_rev=src.rev)
    monkeypatch.undo()
    assert kb.index.links_to(target.page.id) == []  # row and links rolled back together
    await scan(kb)
    assert kb.index.links_to(target.page.id) == [src.page.id]


async def test_many_same_titled_pages_never_share_a_file(kb: KbService) -> None:
    pages = [await kb.write(PageDraft(None, "Same", N, f"body {i}")) for i in range(5)]
    paths = {kb.resolve(p.page.id) for p in pages}
    assert len(paths) == 5
    for i, p in enumerate(pages):
        assert (await kb.read(p.page.id)).page.body == f"body {i}"


async def test_external_save_during_write_is_not_overwritten(
    kb: KbService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jesinia.services.kb.infrastructure.fs import LocalFiles

    p = await kb.write(PageDraft(None, "Race", N, "v1"))
    f = kb.root / "Notes/Race.md"  # type: ignore[operator]
    original = LocalFiles.write_atomic

    def editor_saves_first(self: LocalFiles, disk: str, data: bytes, **kw: object) -> float:
        f.write_text(f.read_text().replace("v1", "user"))
        return original(self, disk, data, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(LocalFiles, "write_atomic", editor_saves_first)
    with pytest.raises(StaleWrite):
        await kb.update_body(p.page.id, "agent", expected_rev=p.rev)
    with pytest.raises(StaleWrite):
        await kb.write(PageDraft(p.page.id, "Race", N, "agent"), expected_rev=p.rev)
    assert "user" in f.read_text() and "agent" not in f.read_text()


async def test_failed_move_keeps_page_readable_at_old_path(
    kb: KbService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jesinia.services.kb.infrastructure.fs import LocalFiles

    p = await kb.write(PageDraft(None, "Before", N, "body"))

    def full_disk(self: LocalFiles, src: str, dst: str) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(LocalFiles, "rename", full_disk)
    with pytest.raises(OSError):
        await kb.move(p.page.id, N, title="After")
    r = await kb.read(p.page.id)
    assert r.page.path == PurePosixPath("Notes/Before.md") and r.page.title == "After"


async def test_unavailable_carries_the_reason_given_with_the_files(kb_unset: KbService) -> None:
    kb_unset.set_files(None, "KB folder not found: /Volumes/Gone")
    with pytest.raises(KbUnavailable, match="KB folder not found: /Volumes/Gone"):
        kb_unset.require_available()
    with pytest.raises(KbUnavailable, match="KB folder not found"):
        await kb_unset.write(PageDraft(None, "x", N, ""))
    kb_unset.set_files(None)
    with pytest.raises(KbUnavailable, match="no KB path is configured"):
        kb_unset.require_available()


async def test_write_asset_stores_beside_the_page_id_and_keeps_local_folders_out_of_git(
    kb: KbService,
) -> None:
    page = await kb.write(PageDraft(None, "With picture", N, "x"))
    kb.require_available()  # a configured KB passes
    rel = await kb.write_asset(page.page.id, "ab12.png", b"\x89PNG", local_only=True)
    assert rel == PurePosixPath(f"_assets/{page.page.id}/ab12.png")
    folder = kb.root / "_assets" / page.page.id  # type: ignore[operator]
    assert (folder / "ab12.png").read_bytes() == b"\x89PNG"
    assert (folder / ".gitignore").read_text() == "*\n"
    await kb.write_asset(page.page.id, "cd34.jpg", b"jpg")  # shared folder stays local
    assert (folder / ".gitignore").read_text() == "*\n"


@pytest.mark.parametrize(
    "name", ["", ".gitignore", "../x.png", "a/b.png", ".hidden.png", "x.png/", "x\\y.png", "a b"]
)
async def test_write_asset_refuses_unsafe_names(kb: KbService, name: str) -> None:
    page = await kb.write(PageDraft(None, "P", N, "x"))
    with pytest.raises(InvalidAssetName):
        await kb.write_asset(page.page.id, name, b"x")


async def test_write_asset_needs_an_indexed_page(kb: KbService) -> None:
    with pytest.raises(PageNotFound):
        await kb.write_asset(PageId("../../etc"), "a.png", b"x")


async def test_read_asset_is_pinned_to_the_page_folder_in_the_url(kb: KbService) -> None:
    root = kb.root
    assert root is not None
    page_a = "0199e000-0000-7000-8000-00000000000a"
    page_b = "0199e000-0000-7000-8000-00000000000b"
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    (root / "_assets" / page_b).mkdir(parents=True)
    (root / "_assets" / page_b / "x.png").write_bytes(png)
    assert (await kb.read_asset(page_b, "x.png")).data == png
    # Page A's folder is a link to B's: A's URL must not reach B's files.
    (root / "_assets" / page_a).symlink_to(root / "_assets" / page_b)
    with pytest.raises(PathOutsideKb, match="not in"):
        await kb.read_asset(page_a, "x.png")


# --- agent-only pages and folders (ADR 0011) -----------------------------------------------

FOLDER_LOCK = {"kind": "folder", "access": "agents"}
MEMORY = PurePosixPath("91 Memory")


async def test_should_lock_every_page_under_an_agent_only_folder_page(kb: KbService) -> None:
    await kb.write(PageDraft(None, "Memory", MEMORY, "Agents' memory.", FOLDER_LOCK))
    deep = await kb.write(PageDraft(None, "Prefs", MEMORY / "claude", "x"))
    open_page = await kb.write(PageDraft(None, "Open", N, "x"))

    assert kb.is_agent_only(deep.page.id)
    assert not kb.is_agent_only(open_page.page.id)
    assert not kb.is_agent_only(PageId("0192f7a8-0000-7000-8000-0000000000ff"))
    assert kb.agent_only().covers_folder(MEMORY / "new")
    with pytest.raises(AgentOnlyPage):
        kb.require_user_access(deep.page.id)
    with pytest.raises(AgentOnlyPage):
        kb.require_user_folder(MEMORY)
    kb.require_user_access(open_page.page.id)
    kb.require_user_folder(N)
    # Agents (and the service itself) still read and write locked pages.
    updated = await kb.update_body(deep.page.id, "y", expected_rev=deep.rev)
    assert (await kb.read(deep.page.id)).page.body == "y" and updated.page.body == "y"


async def test_should_lock_a_single_page_without_its_folder(kb: KbService) -> None:
    locked = await kb.write(PageDraft(None, "Secret", N, "x", {"access": "agents"}))
    sibling = await kb.write(PageDraft(None, "Open", N, "x"))
    assert kb.is_agent_only(locked.page.id) and not kb.is_agent_only(sibling.page.id)


async def test_should_unlock_when_the_marker_is_removed(kb: KbService) -> None:
    page = await kb.write(PageDraft(None, "Memory", MEMORY, "x", FOLDER_LOCK))
    await kb.write(PageDraft(page.page.id, "Memory", MEMORY, "x", {}), expected_rev=page.rev)
    assert not kb.agent_only()


async def test_tree_should_hide_agent_only_pages_and_emptied_folders_on_request(
    kb: KbService,
) -> None:
    await kb.write(PageDraft(None, "Memory", MEMORY, "x", FOLDER_LOCK))
    await kb.write(PageDraft(None, "Prefs", MEMORY / "claude", "x"))
    await kb.write(PageDraft(None, "Secret", N, "x", {"access": "agents"}))
    await kb.write(PageDraft(None, "Open", N, "x"))

    full = await kb.tree()
    assert [n.path.name for n in full] == ["91 Memory", "Notes"] and full[1].pages == 2
    visible = await kb.tree(hide_agent_only=True)
    assert [n.path.name for n in visible] == ["Notes"]
    assert [c.title for c in visible[0].children] == ["Open"] and visible[0].pages == 1
