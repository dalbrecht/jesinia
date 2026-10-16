# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import PurePosixPath

from jesinia.core.events import Event
from jesinia.services.kb.application.service import PageDraft
from jesinia.services.search.application.service import ExternalRecord
from tests.services.search.conftest import Env

N = PurePosixPath("Notes")


async def test_finds_kb_page_and_marks_authoritative(env: Env) -> None:
    await env.kb.write(PageDraft(None, "Quantum Notes", N, "entanglement basics"))
    hits = await env.search.query("entanglement")
    assert hits[0].title == "Quantum Notes" and hits[0].authoritative and hits[0].source == "kb"
    assert hits[0].path == "Notes/Quantum Notes.md"
    assert "<mark>entanglement</mark>" in hits[0].snippet


async def test_external_records_separate_and_filterable(env: Env) -> None:
    await env.kb.write(PageDraft(None, "Q", N, "entanglement basics"))
    await env.search.register(
        ExternalRecord("rss", "i1", "Feed item", "entanglement news", "https://x")
    )
    hits = await env.search.query("entanglement")
    assert {h.source for h in hits} >= {"rss", "kb"}
    rss = next(h for h in hits if h.source == "rss")
    assert not rss.authoritative and rss.url == "https://x" and rss.path is None
    assert all(h.source == "kb" for h in await env.search.query("entanglement", sources={"kb"}))
    await env.search.unregister("rss", "i1")
    assert all(h.source == "kb" for h in await env.search.query("entanglement"))


async def test_register_replaces_same_key(env: Env) -> None:
    await env.search.register(ExternalRecord("rss", "i1", "Old", "alpha", None))
    await env.search.register(ExternalRecord("rss", "i1", "New", "beta", None))
    assert await env.search.query("alpha") == []
    assert [h.title for h in await env.search.query("beta")] == ["New"]


async def test_diacritics_insensitive(env: Env) -> None:
    await env.kb.write(PageDraft(None, "Café culture", N, "espresso"))
    assert [h.title for h in await env.search.query("cafe")] == ["Café culture"]


async def test_stemming_and_prefix(env: Env) -> None:
    await env.kb.write(PageDraft(None, "Budgets", N, "maintenance funding models"))
    assert await env.search.query("model")  # porter stemming
    assert await env.search.query("mainten")  # last term is a prefix


async def test_fts_syntax_in_query_is_escaped(env: Env) -> None:
    await env.kb.write(PageDraft(None, "X", N, "plain words"))
    assert await env.search.query('"unbalanced') == []
    assert await env.search.query("NOT OR AND (") == []
    assert await env.search.query("   ") == []
    assert await env.search.query("words*") != []


async def test_title_ranks_above_body(env: Env) -> None:
    await env.kb.write(PageDraft(None, "Other", N, "maintenance mentioned once"))
    await env.kb.write(PageDraft(None, "Maintenance", N, "about upkeep"))
    assert [h.title for h in await env.search.query("maintenance")][0] == "Maintenance"


async def test_snippet_escapes_html(env: Env) -> None:
    await env.kb.write(PageDraft(None, "H", N, "<script>alert(1)</script> keyword here"))
    snip = (await env.search.query("keyword"))[0].snippet
    assert "<script>" not in snip and "&lt;script&gt;" in snip and "<mark>keyword</mark>" in snip


async def test_follows_kb_events(env: Env) -> None:
    p = await env.kb.write(PageDraft(None, "Live", N, "firstword"))
    q = await env.kb.update_body(p.page.id, "secondword", expected_rev=p.rev)
    assert await env.search.query("firstword") == []
    assert [h.key for h in await env.search.query("secondword")] == [p.page.id]
    await env.kb.move(q.page.id, PurePosixPath("Archive"), title="Moved")
    assert (await env.search.query("secondword"))[0].path == "Archive/Moved.md"
    await env.bus.publish(
        Event("kb.page_changed", {"id": p.page.id, "path": "x", "change": "missing"})
    )
    assert await env.search.query("secondword") == []


async def test_reindex_rebuilds_from_kb(env: Env) -> None:
    await env.kb.write(PageDraft(None, "A", N, "alpha"))
    await env.kb.write(PageDraft(None, "B", N, "beta"))
    await env.search.register(ExternalRecord("rss", "i1", "Keep", "alpha", None))
    assert await env.search.reindex() == 2
    assert {h.source for h in await env.search.query("alpha")} == {"kb", "rss"}


async def test_limit(env: Env) -> None:
    for i in range(5):
        await env.kb.write(PageDraft(None, f"Page {i}", N, "common term"))
    assert len(await env.search.query("common", limit=3)) == 3


def test_malformed_fts_query_returns_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jesinia.core.db import open_db
    from jesinia.services.search.infrastructure.fts import FtsIndex
    from tests.services.search.conftest import SEARCH_DB

    conn = open_db(tmp_path, SEARCH_DB)
    assert FtsIndex(conn).match('"unbalanced', 5, None) == []
    conn.close()
