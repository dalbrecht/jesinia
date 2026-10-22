# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import shutil
import unicodedata
from pathlib import PurePosixPath

from jesinia.core.events import Event
from jesinia.services.kb.application.scan import scan
from jesinia.services.kb.application.service import KbService, PageDraft
from jesinia.services.kb.domain.frontmatter import parse_document

N = PurePosixPath("Notes")


def _root(kb: KbService):  # type: ignore[no-untyped-def]
    assert kb.root is not None
    return kb.root


async def test_plain_file_adopted_with_filename_title(kb: KbService) -> None:
    (_root(kb) / "Inbox").mkdir()
    (_root(kb) / "Inbox/My Idea.md").write_text("text\n")
    report = await scan(kb)
    doc = parse_document((_root(kb) / "Inbox/My Idea.md").read_text())
    assert report.adopted == [PurePosixPath("Inbox/My Idea.md")]
    assert doc.frontmatter is not None and doc.frontmatter["title"] == "My Idea"
    assert doc.body == "text\n"


async def test_foreign_frontmatter_untouched(kb: KbService) -> None:
    (_root(kb) / "post.md").write_text("---\nlayout: post\n---\nx")
    before = (_root(kb) / "post.md").read_bytes()
    report = await scan(kb)
    assert (_root(kb) / "post.md").read_bytes() == before and report.foreign_skipped == 1


async def test_reserved_and_hidden_plain_files_not_adopted(kb: KbService) -> None:
    for rel in ("Journal/day.md", ".obsidian/x.md", "_assets/1/a.md"):
        (_root(kb) / rel).parent.mkdir(parents=True, exist_ok=True)
        (_root(kb) / rel).write_text("plain\n")
    report = await scan(kb)
    assert report.adopted == [] and (_root(kb) / "Journal/day.md").read_text() == "plain\n"


async def test_external_rename_is_move_not_new_page(kb: KbService, events: list[Event]) -> None:
    p = await kb.write(PageDraft(None, "A", N, "b"))
    (_root(kb) / "Notes/A.md").rename(_root(kb) / "Notes/B.md")
    report = await scan(kb)
    assert kb.resolve(p.page.id) == PurePosixPath("Notes/B.md") and report.adopted == []
    assert events[-1].payload == {
        "id": p.page.id,
        "path": "Notes/B.md",
        "change": "moved",
        "origin": "external",
    }


async def test_external_edit_emits_updated(kb: KbService, events: list[Event]) -> None:
    p = await kb.write(PageDraft(None, "E", N, "v1"))
    f = _root(kb) / "Notes/E.md"
    f.write_text(f.read_text().replace("v1", "v2"))
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    await scan(kb)
    assert events[-1].payload["change"] == "updated" and events[-1].payload["id"] == p.page.id
    assert (await kb.read(p.page.id)).page.body == "v2"


async def test_nfd_filename_matches_by_id(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "Café", N, "b"))
    nfd = unicodedata.normalize("NFD", "Notes/Café.md")
    os.rename(_root(kb) / "Notes/Café.md", _root(kb) / nfd)
    r1 = await scan(kb)
    r2 = await scan(kb)
    assert r1.adopted == [] and kb.resolve(p.page.id) is not None and r2.indexed == r1.indexed
    assert (await kb.read(p.page.id)).page.body == "b"


async def test_vanished_file_reported_never_deleted(kb: KbService, events: list[Event]) -> None:
    p = await kb.write(PageDraft(None, "A", N, "b"))
    (_root(kb) / "Notes/A.md").unlink()
    assert (await scan(kb)).missing == [p.page.id]
    assert events[-1].payload["change"] == "missing"
    conflicts = await kb.conflicts()
    assert [(c.kind, c.id) for c in conflicts] == [("missing", p.page.id)]


async def test_duplicate_ids_reported_not_deleted(kb: KbService) -> None:
    await kb.write(PageDraft(None, "A", N, "b"))
    shutil.copy(_root(kb) / "Notes/A.md", _root(kb) / "Notes/A copy.md")
    report = await scan(kb)
    assert len(report.duplicates[0].paths) == 2 and (_root(kb) / "Notes/A copy.md").exists()
    assert [c.kind for c in await kb.conflicts()] == ["duplicate_id"]
    (_root(kb) / "Notes/A copy.md").unlink()
    assert (await scan(kb)).duplicates == [] and await kb.conflicts() == []


async def test_scan_is_incremental_and_counts(kb: KbService) -> None:
    await kb.write(PageDraft(None, "One", N, ""))
    await kb.write(PageDraft(None, "Two", N, ""))
    (_root(kb) / "notes.txt").write_text("ignored")
    assert (await scan(kb)).indexed == 2
    assert (await scan(kb)).indexed == 2


async def test_symlinked_files_and_dirs_are_never_adopted(kb: KbService, tmp_path) -> None:  # type: ignore[no-untyped-def]
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("plain outside file\n")
    (_root(kb) / "link.md").symlink_to(secret)
    (_root(kb) / "linkdir").symlink_to(outside)
    report = await scan(kb)
    assert report.adopted == [] and secret.read_text() == "plain outside file\n"


async def test_one_bad_file_does_not_stop_the_scan(kb: KbService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (_root(kb) / "good.md").write_text("fine\n")
    (_root(kb) / "bad.md").write_text("broken\n")
    files = kb.files
    assert files is not None
    real_read = files.read

    def read(disk: str) -> bytes:
        if disk == "bad.md":
            raise PermissionError("denied")
        return real_read(disk)

    monkeypatch.setattr(files, "read", read)
    report = await scan(kb)
    assert report.adopted == [PurePosixPath("good.md")]
    assert report.errors == [(PurePosixPath("bad.md"), "denied")]


async def test_bad_id_frontmatter_is_reported(kb: KbService) -> None:
    (_root(kb) / "odd.md").write_text("---\nid: x\njesinia: [1]\n---\nbody\n")
    report = await scan(kb)
    assert [p for p, _ in report.errors] == [PurePosixPath("odd.md")]


async def test_invalid_utf8_is_reported_not_rewritten(kb: KbService) -> None:
    raw = b"caf\xe9 latin-1 bytes\n"
    (_root(kb) / "latin.md").write_bytes(raw)
    report = await scan(kb)
    assert report.adopted == [] and [p for p, _ in report.errors] == [PurePosixPath("latin.md")]
    assert (_root(kb) / "latin.md").read_bytes() == raw


async def test_adopted_page_emits_created(kb: KbService, events: list[Event]) -> None:
    (_root(kb) / "Plain.md").write_text("hello\n")
    await scan(kb)
    assert events[-1].payload["change"] == "created" and events[-1].payload["path"] == "Plain.md"


async def test_unreadable_known_page_is_not_marked_missing(
    kb: KbService, events: list[Event]
) -> None:
    p = await kb.write(PageDraft(None, "Fragile", N, "ok"))
    f = _root(kb) / "Notes/Fragile.md"
    f.write_bytes(f.read_bytes() + b"\xff")  # invalid UTF-8 now; still on disk
    report = await scan(kb)
    assert [path for path, _ in report.errors] == [PurePosixPath("Notes/Fragile.md")]
    assert report.missing == [] and kb.index.get(p.page.id) is not None
    assert await kb.conflicts() == []
    assert not [e for e in events if e.payload.get("change") == "missing"]


async def test_path_taken_over_by_another_page_reports_missing(
    kb: KbService, events: list[Event]
) -> None:
    old = await kb.write(PageDraft(None, "A", N, "original"))
    (_root(kb) / "Notes/A.md").write_text("replaced by a plain file\n")
    report = await scan(kb)
    assert report.missing == [old.page.id] and report.adopted == [PurePosixPath("Notes/A.md")]
    assert any(
        e.payload.get("change") == "missing" and e.payload["id"] == old.page.id for e in events
    )
    assert [c.id for c in await kb.conflicts()] == [old.page.id]


async def test_adoption_skips_file_changed_during_write(kb: KbService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from jesinia.services.kb.infrastructure.fs import LocalFiles

    f = _root(kb) / "plain.md"
    f.write_text("draft\n")
    original = LocalFiles.write_atomic

    def typing(self: LocalFiles, disk: str, data: bytes, **kw: object) -> float:
        f.write_text("draft, still typing\n")
        return original(self, disk, data, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(LocalFiles, "write_atomic", typing)
    report = await scan(kb)
    assert report.adopted == [] and f.read_text() == "draft, still typing\n"


async def test_should_pick_up_an_agent_only_marker_written_outside_jesinia(
    kb: KbService,
) -> None:
    folder = _root(kb) / "93 Scratch"
    folder.mkdir()
    (folder / "Scratch.md").write_text(
        "---\nid: 0192f7a8-0000-7000-8000-000000000093\ntitle: Scratch\n"
        "jesinia:\n  kind: folder\n  access: agents\n---\nAgents only.\n"
    )
    (folder / "Draft.md").write_text("plain\n")
    await scan(kb)
    assert kb.agent_only().covers(PurePosixPath("93 Scratch/Draft.md"))

    # Removing the marker by hand unlocks the folder on the next scan.
    text = (folder / "Scratch.md").read_text().replace("  access: agents\n", "")
    (folder / "Scratch.md").write_text(text)
    os.utime(folder / "Scratch.md", (1, 1))
    await scan(kb)
    assert not kb.agent_only()
