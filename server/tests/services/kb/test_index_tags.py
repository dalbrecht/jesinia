# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import PurePosixPath

from jesinia.services.kb.application.scan import scan
from jesinia.services.kb.application.service import KbService, PageDraft
from jesinia.services.kb.domain.page import PageId, source_of

N = PurePosixPath("Notes")


def _root(kb: KbService):  # type: ignore[no-untyped-def]
    assert kb.root is not None
    return kb.root


async def test_write_indexes_tags_and_source(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "A", N, "hello #a #b", {"source": "https://x.test/u"}))
    assert kb.index.tag_counts() == {"a": 1, "b": 1}
    assert kb.index.by_source("https://x.test/u") == p.page.id
    assert kb.index.pages_with_tag("a") == [p.page.id]
    assert kb.index.by_source("https://nope.test") is None


async def test_rewrite_replaces_tags_and_clears_source(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "A", N, "#a", {"source": "https://x.test/u"}))
    await kb.write(PageDraft(p.page.id, "A", N, "#c"), expected_rev=p.rev)
    assert kb.index.tag_counts() == {"c": 1}
    assert kb.index.by_source("https://x.test/u") is None


async def test_scan_indexes_hand_edited_tags(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "A", N, "#a"))
    file = _root(kb) / "Notes/A.md"
    file.write_text(file.read_text().replace("#a", "#a #z"))
    await scan(kb)
    assert kb.index.tag_counts() == {"a": 1, "z": 1}
    assert kb.index.pages_with_tag("z") == [p.page.id]


async def test_scan_indexes_source_from_front_matter(kb: KbService) -> None:
    (_root(kb) / "S.md").write_text(
        "---\nid: 01HZZZZZZZZZZZZZZZZZZZZZZZ\ntitle: S\njesinia:\n  source: https://s.test/a\n---\n#t\n"
    )
    await scan(kb)
    assert kb.index.tag_counts() == {"t": 1}
    assert kb.index.by_source("https://s.test/a") is not None


async def test_reserved_folders_are_not_indexed(kb: KbService) -> None:
    (_root(kb) / "Journal").mkdir()
    (_root(kb) / "Journal/x.md").write_text("#a\n")
    await scan(kb)
    assert kb.index.tag_counts() == {}


async def test_delete_and_reset_clear_tags_and_sources(kb: KbService) -> None:
    p = await kb.write(PageDraft(None, "A", N, "#a", {"source": "https://x.test/u"}))
    kb.index.delete(p.page.id)
    assert kb.index.tag_counts() == {} and kb.index.by_source("https://x.test/u") is None
    kb.index.set_tags(PageId("q"), ["t"])
    kb.index.set_source(PageId("q"), "https://y.test")
    kb.index.reset()
    assert kb.index.tag_counts() == {} and kb.index.by_source("https://y.test") is None


def test_source_of_requires_a_non_empty_string() -> None:
    assert source_of({"source": "u"}) == "u"
    assert source_of({"source": ""}) is None
    assert source_of({"source": 3}) is None
    assert source_of({}) is None


async def test_catalog_page_does_not_count_as_a_use(kb: KbService) -> None:
    (_root(kb) / "Tags.md").write_text(
        "---\nid: 01HZZZZZZZZZZZZZZZZZZZZZZY\ntitle: Tags\njesinia:\n  kind: tag-catalog\n---\n"
        "- #approved: nobody uses this yet\n"
    )
    (_root(kb) / "P.md").write_text("---\nid: 01HZZZZZZZZZZZZZZZZZZZZZZX\ntitle: P\n---\n#used\n")
    await scan(kb)
    assert kb.index.tag_counts() == {"used": 1}


async def test_pages_by_source_lists_every_page_with_the_url(kb: KbService) -> None:
    a = await kb.write(PageDraft(None, "A", N, "a", {"source": "https://x.test/u"}))
    b = await kb.write(PageDraft(None, "B", N, "b", {"source": "https://x.test/u"}))
    assert kb.index.pages_by_source("https://x.test/u") == sorted([a.page.id, b.page.id])
    assert kb.index.pages_by_source("https://nope.test") == []
