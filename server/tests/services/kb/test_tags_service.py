# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path, PurePosixPath

import pytest

from jesinia.services.kb.application.errors import (
    InvalidTag,
    KbUnavailable,
    NearDuplicate,
    StaleWrite,
    TagExists,
    TagNotFound,
)
from jesinia.services.kb.application.scan import scan
from jesinia.services.kb.application.service import KbService, PageDraft
from jesinia.services.kb.application.tags import (
    PageRef,
    RenameResult,
    Suggestion,
    TagCheck,
    TagInfo,
    TagService,
)
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.tag_catalog import CatalogEntry

N = PurePosixPath("Notes")


class FakeAdvisor:
    def __init__(
        self, tags: list[str] | None = None, meaning: str | None = None, fail: bool = False
    ) -> None:
        self.tags = tags or []
        self.meaning = meaning
        self.fail = fail
        self.seen: list[list[CatalogEntry]] = []

    async def suggest(self, text: str, catalog: list[CatalogEntry]) -> list[str]:
        self.seen.append(catalog)
        if self.fail:
            raise RuntimeError("runner exploded")
        return list(self.tags)

    async def same_meaning(self, tag: str, catalog: list[CatalogEntry]) -> str | None:
        if self.fail:
            raise RuntimeError("runner exploded")
        return self.meaning


def _root(kb: KbService) -> Path:
    assert kb.root is not None
    return kb.root


async def _page(kb: KbService, title: str, body: str, folder: PurePosixPath = N) -> PageId:
    return (await kb.write(PageDraft(None, title, folder, body))).page.id


async def _catalog_text(kb: KbService) -> str:
    return (_root(kb) / "Tags.md").read_text()


async def _edit_catalog(kb: KbService, old: str, new: str) -> None:
    row = kb.index.by_path(PurePosixPath("Tags.md"))
    assert row is not None
    cat = await kb.read(row.id)
    await kb.update_body(row.id, cat.page.body.replace(old, new), expected_rev=cat.rev)


async def test_add_refuses_near_duplicates_unless_forced(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite", "SQLite internals")
    with pytest.raises(TagExists):
        await tags.add("SQLite")
    await tags.add("database")
    with pytest.raises(NearDuplicate) as near:
        await tags.add("databases")
    assert near.value.near == ["database"]
    await tags.add("databases", force=True)
    with pytest.raises(TagExists):
        await tags.add("Databases", force=True)
    assert [t.tag for t in await tags.catalog()] == ["sqlite", "database", "databases"]


async def test_first_add_creates_the_catalog_page_at_the_root(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("#Machine Learning", "ML")
    text = await _catalog_text(kb)
    assert "kind: tag-catalog" in text and "title: Tags" in text
    assert text.endswith("- #machine-learning: ML\n")
    await tags.add("wal")
    assert (await _catalog_text(kb)).endswith("- #machine-learning: ML\n- #wal\n")


async def test_add_rejects_invalid_tags(kb: KbService) -> None:
    tags = TagService(kb, None)
    for raw in ("", "#", "_x", "a/b", "-x"):
        with pytest.raises(InvalidTag):
            await tags.add(raw)


async def test_catalog_lists_counts_without_the_catalog_page(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite", "db")
    await tags.add("old")
    await tags.retire("old")
    await tags.ignore("noise")
    await _page(kb, "A", "#sqlite #sql-lite")
    await _page(kb, "B", "#SQL-Lite #old")
    catalog = await tags.catalog()
    assert catalog == [
        TagInfo("sqlite", "db", (), False, 1),
        TagInfo("old", "", (), True, 1),
    ]
    await tags.preview_rename("sql-lite", "sqlite")  # read-only
    assert (await tags.catalog())[0].count == 1


async def test_catalog_counts_pages_using_an_alias(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite")
    await _page(kb, "A", "#sqlite")
    await tags.apply_rename("sql-lite", "sqlite", {})
    await _page(kb, "B", "#sql-lite written later, by hand")
    assert (await tags.catalog())[0] == TagInfo("sqlite", "", ("sql-lite",), False, 2)


async def test_check_reports_exact_alias_and_near_matches(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite")
    await tags.add("database")
    await _edit_catalog(kb, "- #sqlite", "- #sqlite (aliases: sql-lite)")
    assert await tags.check("SQLite") == TagCheck("sqlite", True, "sqlite", [], None)
    assert await tags.check("#SQL Lite") == TagCheck("sql-lite", True, "sqlite", [], None)
    assert await tags.check("databases") == TagCheck("databases", False, None, ["database"], None)
    assert await tags.check("sql_lites") == TagCheck("sql-lites", False, None, ["sqlite"], None)
    with pytest.raises(InvalidTag):
        await tags.check("#")


async def test_check_asks_the_advisor_for_same_meaning(kb: KbService) -> None:
    await TagService(kb, None).add("machine-learning")
    assert (await TagService(kb, FakeAdvisor(meaning="Machine Learning")).check("ml")) == TagCheck(
        "ml", False, None, [], "machine-learning"
    )
    unknown = await TagService(kb, FakeAdvisor(meaning="not-in-catalog")).check("ml")
    assert unknown.same_meaning is None
    failing = await TagService(kb, FakeAdvisor(fail=True)).check("ml")
    assert failing.same_meaning is None
    exact = await TagService(kb, FakeAdvisor(meaning="machine-learning")).check("machine-learning")
    assert exact.same_meaning is None


async def test_suggest_prefers_catalog_and_flags_new(kb: KbService) -> None:
    plain = TagService(kb, None)
    await plain.add("sqlite")
    await plain.add("write-ahead-log")
    await plain.add("python")
    await _edit_catalog(kb, "- #python", "- #python (aliases: py)")
    text = "Notes on the SQLite write ahead log, with some Py scripts."
    assert await plain.suggest(text) == [
        Suggestion("sqlite", False),
        Suggestion("write-ahead-log", False),
        Suggestion("python", False),
    ]
    advised = TagService(kb, FakeAdvisor(["SQLite", "Durability", "py", "a/b", "durability"]))
    assert await advised.suggest(text, limit=4) == [
        Suggestion("sqlite", False),
        Suggestion("write-ahead-log", False),
        Suggestion("python", False),
        Suggestion("durability", True),
    ]
    assert await advised.suggest(text, limit=1) == [Suggestion("sqlite", False)]
    broken = TagService(kb, FakeAdvisor(fail=True))
    assert [s.tag for s in await broken.suggest(text)] == ["sqlite", "write-ahead-log", "python"]


async def test_suggest_never_offers_retired_tags(kb: KbService) -> None:
    advisor = FakeAdvisor(["old", "fresh"])
    tags = TagService(kb, advisor)
    await tags.add("old")
    await tags.retire("old")
    assert await tags.suggest("an old note") == [Suggestion("fresh", True)]
    assert advisor.seen[-1] == []


async def test_suggest_without_a_catalog_returns_advisor_tags(kb: KbService) -> None:
    assert await TagService(kb, None).suggest("anything") == []
    assert await TagService(kb, FakeAdvisor(["x"])).suggest("anything") == [Suggestion("x", True)]


async def test_rename_rewrites_pages_and_adds_alias(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqllite", "typo")
    a = await _page(kb, "A", "about #SQLLite and `#sqllite` code")
    b = await _page(kb, "B", "#sqllite\n#other")
    await _page(kb, "C", "#unrelated")
    preview = await tags.preview_rename("sqllite", "SQLite")
    assert sorted(p.id for p in preview.pages) == sorted([a, b])
    assert all(isinstance(p, PageRef) and p.title in {"A", "B"} for p in preview.pages)
    assert preview.skipped_reserved == []
    result = await tags.apply_rename("sqllite", "SQLite", {p.id: p.rev for p in preview.pages})
    assert sorted(result.updated) == sorted([a, b]) and result.skipped_changed == []
    assert (await kb.read(a)).page.body == "about #sqlite and `#sqllite` code"
    assert (await kb.read(b)).page.body == "#sqlite\n#other"
    catalog = await tags.catalog()
    assert catalog == [TagInfo("sqlite", "typo", ("sqllite",), False, 2)]
    assert (await tags.check("sqllite")).canonical == "sqlite"


async def test_rename_into_an_existing_tag_merges_aliases(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite", "db engine")
    await tags.add("sqlite3", force=True)
    await _edit_catalog(kb, "- #sqlite3", "- #sqlite3 (aliases: sq3)")
    await tags.apply_rename("sqlite3", "sqlite", {})
    assert await tags.catalog() == [TagInfo("sqlite", "db engine", ("sqlite3", "sq3"), False, 0)]


async def test_rename_rejects_same_or_invalid_names(kb: KbService) -> None:
    tags = TagService(kb, None)
    with pytest.raises(InvalidTag):
        await tags.preview_rename("a", "A")
    with pytest.raises(InvalidTag):
        await tags.apply_rename("a", "b/c", {})
    with pytest.raises(InvalidTag):
        await tags.merge([], "a", {})


async def test_rename_into_an_alias_uses_its_canonical_tag(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite")
    await tags.apply_rename("sql-lite", "sqlite", {})
    a = await _page(kb, "A", "#sqlite3")
    preview = await tags.preview_rename("sqlite3", "sql-lite")
    await tags.apply_rename("sqlite3", "sql-lite", {p.id: p.rev for p in preview.pages})
    assert (await kb.read(a)).page.body == "#sqlite"
    assert (await tags.catalog())[0].aliases == ("sql-lite", "sqlite3")


async def test_rename_skips_pages_changed_since_preview(kb: KbService) -> None:
    tags = TagService(kb, None)
    a = await _page(kb, "A", "#old one")
    b = await _page(kb, "B", "#old two")
    preview = await tags.preview_rename("old", "new")
    current = await kb.read(b)
    await kb.update_body(b, "#old two, edited", expected_rev=current.rev)
    late = await _page(kb, "Late", "#old late")
    result = await tags.apply_rename("old", "new", {p.id: p.rev for p in preview.pages})
    assert result == RenameResult([a], sorted([b, late]))
    assert (await kb.read(b)).page.body == "#old two, edited"
    assert (await kb.read(late)).page.body == "#old late"


async def test_rename_reports_a_page_that_vanished_as_changed(kb: KbService) -> None:
    tags = TagService(kb, None)
    a = await _page(kb, "A", "#old")
    preview = await tags.preview_rename("old", "new")
    (_root(kb) / "Notes/A.md").unlink()
    result = await tags.apply_rename("old", "new", {p.id: p.rev for p in preview.pages})
    assert result == RenameResult([], [a])


async def test_rename_reports_a_race_on_write_as_changed(
    kb: KbService, monkeypatch: pytest.MonkeyPatch
) -> None:
    tags = TagService(kb, None)
    a = await _page(kb, "A", "#old")
    preview = await tags.preview_rename("old", "new")

    async def stale(*args: object, **kwargs: object) -> None:
        raise StaleWrite("raced")

    monkeypatch.setattr(kb, "update_body", stale)
    result = await tags.apply_rename("old", "new", {p.id: p.rev for p in preview.pages})
    assert result == RenameResult([], [a])


async def test_rename_leaves_reserved_folders_and_reports_them(kb: KbService) -> None:
    tags = TagService(kb, None)
    a = await _page(kb, "A", "#old")
    (_root(kb) / "Journal").mkdir()
    (_root(kb) / "Journal/day.md").write_text("plain #old\n")
    (_root(kb) / "Journal/other.md").write_text("plain #new\n")
    (_root(kb) / "Drafts").mkdir()
    indexed = "---\nid: 0192f7a8-0000-7000-8000-00000000000d\ntitle: D\n---\n#OLD draft\n"
    (_root(kb) / "Drafts/d.md").write_text(indexed)
    (_root(kb) / "Drafts/bad.md").write_bytes(b"\xff\xfe #old")
    await scan(kb)
    drafted = PageId("0192f7a8-0000-7000-8000-00000000000d")
    assert drafted in kb.index.pages_with_tag("old")
    preview = await tags.preview_rename("old", "new")
    assert [p.id for p in preview.pages] == [a]
    assert preview.skipped_reserved == ["Drafts/d.md", "Journal/day.md"]
    revs = {p.id: p.rev for p in preview.pages}
    drafted_row = kb.index.get(drafted)
    assert drafted_row is not None
    revs[drafted] = drafted_row.rev
    result = await tags.apply_rename("old", "new", revs)
    assert result == RenameResult([a], [])
    assert (_root(kb) / "Journal/day.md").read_text() == "plain #old\n"
    assert (_root(kb) / "Drafts/d.md").read_text() == indexed


async def test_merge_rewrites_every_source_in_one_write(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("ml", "machine learning")
    await tags.add("machine-learning")
    both = await _page(kb, "Both", "#ml and #deep-learning")
    revs = {both: (await kb.read(both)).rev}
    result = await tags.merge(["ml", "deep-learning", "machine-learning"], "machine-learning", revs)
    assert result == RenameResult([both], [])
    assert (await kb.read(both)).page.body == "#machine-learning and #machine-learning"
    assert await tags.catalog() == [
        TagInfo("machine-learning", "machine learning", ("ml", "deep-learning"), False, 1)
    ]


async def test_rename_of_an_ignored_target_approves_it(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.ignore("noise")
    await tags.apply_rename("racket", "noise", {})
    assert await tags.catalog() == [TagInfo("noise", "", ("racket",), False, 0)]


async def test_uncataloged_lists_hashtags_missing_from_catalog(kb: KbService) -> None:
    tags = TagService(kb, None)
    assert await tags.uncataloged() == []
    await tags.add("sqlite")
    await tags.apply_rename("sql-lite", "sqlite", {})
    await _page(kb, "A", "#sqlite #wal #rust")
    await _page(kb, "B", "#sql-lite #rust")
    await _page(kb, "C", "#Rust")
    assert await tags.uncataloged() == [("rust", 3), ("wal", 1)]
    await tags.adopt("wal")
    assert await tags.uncataloged() == [("rust", 3)]
    assert [t.tag for t in await tags.catalog()] == ["sqlite", "wal"]


async def test_ignored_tags_leave_uncataloged_and_are_never_suggested(kb: KbService) -> None:
    tags = TagService(kb, FakeAdvisor(["noise", "Noise", "keep"]))
    await _page(kb, "A", "#noise #keep")
    await tags.ignore("Noise")
    assert "- #noise (ignored)" in await _catalog_text(kb)
    assert await tags.uncataloged() == [("keep", 1)]
    assert await tags.suggest("noise everywhere") == [Suggestion("keep", True)]
    assert await tags.catalog() == []
    await tags.add("noise", "un-ignored")
    assert [t.tag for t in await tags.catalog()] == ["noise"]


async def test_retire_adopt_and_ignore_guard_their_inputs(kb: KbService) -> None:
    tags = TagService(kb, None)
    with pytest.raises(TagNotFound):
        await tags.retire("ghost")
    await tags.add("real")
    with pytest.raises(TagExists):
        await tags.adopt("Real")
    with pytest.raises(TagExists):
        await tags.ignore("real")
    await tags.retire("REAL")
    assert (await tags.catalog())[0].retired


async def test_catalog_page_is_found_after_a_title_collision(kb: KbService) -> None:
    await kb.write(PageDraft(None, "Tags", PurePosixPath(""), "my own page about #tags"))
    tags = TagService(kb, None)
    await tags.add("first")
    await tags.add("second")
    assert [t.tag for t in await tags.catalog()] == ["first", "second"]
    assert (_root(kb) / "Tags.md").read_text().endswith("my own page about #tags")


async def test_catalog_skips_an_unreadable_candidate(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("first")
    (_root(kb) / "Tags.md").unlink()
    assert await tags.catalog() == []


async def test_an_unconfigured_kb_is_unavailable(kb_unset: KbService) -> None:
    tags = TagService(kb_unset, None)
    assert await tags.catalog() == []
    with pytest.raises(KbUnavailable):
        await tags.add("x")
    assert (await tags.preview_rename("a", "b")).skipped_reserved == []


async def test_add_keeps_meanings_on_one_line_that_reads_back(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("wal", "  write-ahead\n log (the journal) ")
    assert (await tags.catalog())[0].meaning == "write-ahead log (the journal)"
    with pytest.raises(InvalidTag):
        await tags.add("other", "looks like a flag (retired)")


async def test_hashtags_only_in_the_catalog_page_prose_are_not_uncataloged(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("wal")
    await _edit_catalog(kb, "- #wal", "Free text mentioning #misc.\n- #wal")
    assert await tags.uncataloged() == []


async def test_rename_leaves_a_page_alone_when_the_index_was_stale(kb: KbService) -> None:
    tags = TagService(kb, None)
    a = await _page(kb, "A", "no tags here")
    kb.index.set_tags(a, ["old"])
    preview = await tags.preview_rename("old", "new")
    result = await tags.apply_rename("old", "new", {p.id: p.rev for p in preview.pages})
    assert result == RenameResult([], [])
    assert (await kb.read(a)).page.body == "no tags here"


async def test_merging_an_alias_moves_it_off_its_old_entry(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("db")
    await _edit_catalog(kb, "- #db", "- #db (aliases: database, dbs)")
    await tags.merge(["database"], "storage", {})
    assert await tags.catalog() == [
        TagInfo("db", "", ("dbs",), False, 0),
        TagInfo("storage", "", ("database",), False, 0),
    ]
    assert (await tags.check("database")).canonical == "storage"
    assert (await _catalog_text(kb)).count("database") == 1


async def test_rename_never_rewrites_link_anchors(kb: KbService) -> None:
    tags = TagService(kb, None)
    body = "See [the db](#sqlite) and ?q=#sqlite, but #sqlite is a tag.\n\n    #sqlite code\n"
    a = await _page(kb, "A", body)
    preview = await tags.preview_rename("sqlite", "databases")
    await tags.apply_rename("sqlite", "databases", {p.id: p.rev for p in preview.pages})
    assert (await kb.read(a)).page.body == body.replace("but #sqlite", "but #databases")


async def test_ensure_resolves_aliases_and_adds_new_tags_all_or_nothing(kb: KbService) -> None:
    tags = TagService(kb, None)
    await tags.add("sqlite")
    await tags.merge(["sqlite3"], "sqlite", {})
    before = await _catalog_text(kb)
    assert await tags.ensure(["#SQLite3", "sqlite"]) == ["sqlite"]
    assert await _catalog_text(kb) == before  # nothing new: no write
    with pytest.raises(InvalidTag):
        await tags.ensure(["wal-mode", "no!good"])
    with pytest.raises(NearDuplicate):
        await tags.ensure(["database", "databases"])
    assert [t.tag for t in await tags.catalog()] == ["sqlite"]
    assert await tags.ensure(["wal-mode", "Wal-Mode", "sqlite3", "database"]) == [
        "wal-mode",
        "sqlite",
        "database",
    ]
    assert [t.tag for t in await tags.catalog()] == ["sqlite", "wal-mode", "database"]
