# SPDX-License-Identifier: AGPL-3.0-or-later
from jesinia.services.kb.domain.tag_catalog import (
    CATALOG_KIND,
    CATALOG_TITLE,
    CatalogEntry,
    parse_catalog,
    render_catalog,
    with_entry,
)


def test_catalog_parse_is_lenient_and_round_trips_unknown_lines() -> None:
    body = (
        "Intro text\n- #SQLite: SQLite internals (aliases: sql-lite, sqlite3)\n"
        "<!-- note -->\n- #sqlite: dup\n- #old-tag (retired)\n"
    )
    cat = parse_catalog(body)
    assert cat.entries["sqlite"] == CatalogEntry(
        "sqlite", "SQLite internals", ("sql-lite", "sqlite3")
    )
    assert cat.entries["old-tag"].retired
    assert cat.resolve("SQL-Lite") == "sqlite" and cat.resolve("nope") is None
    out = render_catalog(with_entry(cat, CatalogEntry("wal", "write-ahead log")))
    assert "Intro text" in out and "<!-- note -->" in out and "- #wal: write-ahead log" in out
    assert out.count("#sqlite") == 1


def test_constants() -> None:
    assert (CATALOG_TITLE, CATALOG_KIND) == ("Tags", "tag-catalog")


def test_ignored_entries_do_not_resolve_and_retired_still_do() -> None:
    cat = parse_catalog("- #noise (ignored)\n- #old (retired)\n")
    assert cat.entries["noise"].ignored
    assert cat.resolve("noise") is None
    assert cat.resolve("old") == "old"


def test_unrecognised_lines_survive_byte_for_byte() -> None:
    body = "- #foo bar baz\n  - #nested\n\n- plain\n"
    assert render_catalog(parse_catalog(body)) == body


def test_duplicates_merge_first_meaning_wins_and_aliases_union() -> None:
    cat = parse_catalog("- #a (aliases: x)\n- #a: later (aliases: y, x)\n- #a: third\n")
    assert cat.entries["a"] == CatalogEntry("a", "later", ("x", "y"))


def test_render_round_trips_flags_and_is_stable() -> None:
    body = "- #a: m (aliases: b, c) (retired)\n- #d (ignored)\n- #e\n"
    out = render_catalog(parse_catalog(body))
    assert out == body
    assert render_catalog(parse_catalog(out)) == out


def test_with_entry_replaces_existing_in_place() -> None:
    cat = parse_catalog("- #a: old\nfoo\n- #b\n")
    out = render_catalog(with_entry(cat, CatalogEntry("a", "new")))
    assert out == "- #a: new\nfoo\n- #b\n"


def test_resolve_normalises_input_and_prefers_first_claim() -> None:
    cat = parse_catalog("- #a (aliases: shared)\n- #b (aliases: shared)\n")
    assert cat.resolve("#Shared") == "a"
    assert cat.resolve("A") == "a"


def test_new_entry_is_appended_directly_when_page_has_no_trailing_newline() -> None:
    out = render_catalog(with_entry(parse_catalog("intro"), CatalogEntry("wal")))
    assert out == "intro\n- #wal"
