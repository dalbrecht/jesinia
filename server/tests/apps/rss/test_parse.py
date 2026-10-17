# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jesinia.apps.rss.application.ports import FeedReader
from jesinia.apps.rss.domain.feed import FeedParseError
from jesinia.apps.rss.infrastructure.parse import (
    MAX_ITEMS,
    FeedparserReader,
    discover,
    parse_feed,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_rss2_reads_full_content_and_summary_only_items() -> None:
    feed = parse_feed(load("rss2.xml"), "https://example.com/feed.xml")
    assert feed.title == "Example Blog"
    assert feed.site_url == "https://example.com/"
    full, teaser = feed.items
    assert full.guid == "post-1"
    assert full.url == "https://example.com/full"
    assert full.author == "Ada"
    assert full.published_at == datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    assert full.is_summary is False
    assert full.content_html is not None and "<strong>world</strong>" in full.content_html
    assert "script" not in full.content_html
    assert full.snippet == "Short teaser"
    assert teaser.is_summary is True
    assert teaser.published_at is None
    assert teaser.guid == "https://example.com/teaserTeaser only"
    assert teaser.snippet == "Only a summary"
    assert teaser.discussion_url is None


def test_parse_atom_reads_content_summary_and_dates() -> None:
    feed = parse_feed(load("atom.xml"), "https://atom.example.com/feed.atom")
    assert feed.title == "Atom Example"
    assert feed.site_url == "https://atom.example.com/"
    full, summary = feed.items
    assert full.guid == "urn:uuid:1"
    assert full.author == "Grace"
    assert full.published_at == datetime(2026, 10, 11, 8, 30, tzinfo=UTC)
    assert full.is_summary is False
    assert full.content_html == "<p>Atom <em>content</em></p>"
    assert summary.is_summary is True
    assert summary.published_at == datetime(2026, 10, 10, 8, 30, tzinfo=UTC)


def test_parse_jsonfeed_reads_items() -> None:
    feed = parse_feed(load("feed.json"), "https://json.example.com/feed.json")
    assert feed.title == "JSON Example"
    assert feed.site_url == "https://json.example.com/"
    html_item, text_item, summary_item = feed.items
    assert html_item.guid == "j1"
    assert html_item.author == "Linus"
    assert html_item.published_at == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert html_item.is_summary is False
    assert html_item.content_html is not None and "iframe" not in html_item.content_html
    assert text_item.content_html == "<p>plain body</p>"
    assert text_item.published_at is None
    assert text_item.author is None
    assert summary_item.is_summary is True
    assert summary_item.content_html is None
    assert summary_item.snippet == "only summary"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b'{"title": "no items key"}',
        b'{"version": "https://jsonfeed.org/version/1.1", "items": "nope"}',
        b"[1, 2]",
        b"{not json",
    ],
)
def test_parse_jsonfeed_rejects_malformed_documents(body: bytes) -> None:
    with pytest.raises(FeedParseError):
        parse_feed(body, "https://json.example.com/feed.json")


def test_parse_broken_document_raises_feed_parse_error() -> None:
    with pytest.raises(FeedParseError):
        parse_feed(load("broken.xml"), "https://example.com/broken")


def test_discover_finds_feed_link_alternate() -> None:
    found = discover(load("html-with-link-alternate.html"), "https://example.com/blog/")
    assert found == "https://example.com/feeds/atom.xml"


def test_discover_returns_none_without_feed_link() -> None:
    assert discover(b"<html><head></head></html>", "https://example.com/") is None
    assert discover(b'<link rel="alternate" type="text/html" href="/x">', "https://e.com/") is None
    assert discover(b'<link rel="alternate" type="application/rss+xml">', "https://e.com/") is None


def test_discover_accepts_json_feed_and_rss_types() -> None:
    rss = b'<link rel="ALTERNATE" type="application/rss+xml" href="rss.xml">'
    assert discover(rss, "https://e.com/a/") == "https://e.com/a/rss.xml"
    js = b'<link rel="alternate" type="application/feed+json" href="/f.json">'
    assert discover(js, "https://e.com/a/") == "https://e.com/f.json"


def test_hn_items_link_article_and_discussion() -> None:
    feed = parse_feed(load("hn.xml"), "https://news.ycombinator.com/rss")
    (item,) = feed.items
    assert item.url == "https://thing.example.org/post"
    assert item.discussion_url == "https://news.ycombinator.com/item?id=4242"
    assert item.guid == "https://news.ycombinator.com/item?id=4242"


def test_non_hn_comments_link_is_not_treated_as_discussion() -> None:
    body = load("hn.xml")
    feed = parse_feed(body, "https://other.example.com/rss")
    assert feed.items[0].discussion_url is None


def test_parse_caps_items_and_sanitises_content() -> None:
    entries = "".join(
        f"<item><title>t{n}</title><link>https://e.com/{n}</link><guid>g{n}</guid>"
        f"<pubDate>Mon, 12 Oct 2026 10:{n // 60:02d}:{n % 60:02d} GMT</pubDate>"
        f"<description>{'x' * 400}</description>"
        '<content:encoded><![CDATA[<p onclick="x()">hi</p><script>1</script>]]>'
        "</content:encoded></item>"
        for n in range(MAX_ITEMS + 20)
    )
    body = (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>T</title>'
        f"<link>https://e.com/</link>{entries}</channel></rss>"
    ).encode()
    feed = parse_feed(body, "https://e.com/feed")
    assert len(feed.items) == MAX_ITEMS
    dates = [item.published_at for item in feed.items if item.published_at is not None]
    assert len(dates) == MAX_ITEMS
    assert dates == sorted(dates, reverse=True)
    assert feed.items[0].guid == f"g{MAX_ITEMS + 19}"
    assert all(item.content_html == "<p>hi</p>" for item in feed.items)
    assert all(len(item.snippet) <= 280 for item in feed.items)


def test_snippet_is_truncated_to_280_characters_for_long_summaries() -> None:
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        f"<item><title>a</title><link>https://e.com/a</link><description>{'word ' * 200}"
        "</description></item></channel></rss>"
    ).encode()
    (item,) = parse_feed(body, "https://e.com/feed").items
    assert 0 < len(item.snippet) <= 280


def test_item_without_link_or_title_still_gets_a_guid() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        b"<item><description>d</description></item></channel></rss>"
    )
    (item,) = parse_feed(body, "https://e.com/feed").items
    assert item.url == "https://e.com/feed"
    assert item.title == ""
    assert item.guid == "https://e.com/feed"


def test_feed_without_title_or_site_url_falls_back_to_feed_url() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>a</title><link>https://e.com/a</link></item></channel></rss>"
    )
    feed = parse_feed(body, "https://e.com/feed")
    assert feed.title == "https://e.com/feed"
    assert feed.site_url == "https://e.com/feed"


def test_reader_implements_the_feed_reader_port() -> None:
    reader: FeedReader = FeedparserReader()
    assert reader.parse(load("atom.xml"), "https://a.example.com/f").title == "Atom Example"
    assert reader.discover(load("html-with-link-alternate.html"), "https://e.com/") is not None


def _rss_with_link(link: str, comments: str = "") -> bytes:
    extra = f"<comments>{comments}</comments>" if comments else ""
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        f"<link>{link}</link><item><title>a</title><link>{link}</link>{extra}</item>"
        "</channel></rss>"
    ).encode()


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("javascript:alert(1)", "https://e.com/feed"),
        ("data:text/html,hi", "https://e.com/feed"),
        ("/relative/path", "https://e.com/relative/path"),
        ("//cdn.example.org/x", "https://cdn.example.org/x"),
        ("http://", "https://e.com/feed"),
        # Never copied into a kept page's front matter (and so into git).
        ("https://me:pw@a.example/x", "https://e.com/feed"),
    ],
)
def test_item_and_site_urls_are_normalised_to_http_urls(link: str, expected: str) -> None:
    feed = parse_feed(_rss_with_link(link), "https://e.com/feed")
    assert feed.items[0].url == expected
    assert feed.site_url == expected


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ("javascript:alert(1)", None),
        ("data:text/html,hi", None),
        ("/item?id=1", "https://news.ycombinator.com/item?id=1"),
        ("//news.ycombinator.com/item?id=2", "https://news.ycombinator.com/item?id=2"),
        ("https://u:p@news.ycombinator.com/item?id=3", None),
    ],
)
def test_discussion_url_is_normalised_or_dropped(comments: str, expected: str | None) -> None:
    body = _rss_with_link("https://a.example.org/x", comments)
    feed = parse_feed(body, "https://news.ycombinator.com/rss")
    assert feed.items[0].discussion_url == expected


@pytest.mark.parametrize(
    "href",
    ["javascript:alert(1)", "data:application/rss+xml,x", "http://"],
)
def test_discover_rejects_non_http_feed_links(href: str) -> None:
    page = f'<link rel="alternate" type="application/rss+xml" href="{href}">'.encode()
    assert discover(page, "https://e.com/") is None


def test_discover_resolves_protocol_relative_links() -> None:
    page = b'<link rel="alternate" type="application/rss+xml" href="//f.example.org/x.xml">'
    assert discover(page, "https://e.com/") == "https://f.example.org/x.xml"


def test_truncated_feed_keeps_valid_items_and_reports_warning() -> None:
    body = load("rss2.xml")
    truncated = body[: body.index(b"<item>", body.index(b"</item>")) + 30]
    feed = parse_feed(truncated, "https://example.com/feed.xml")
    assert feed.items[0].guid == "post-1"
    assert feed.warning is not None and feed.warning != ""


def test_clean_feed_has_no_warning() -> None:
    assert parse_feed(load("atom.xml"), "https://a.example.com/f").warning is None


def test_unparseable_document_chains_the_parser_cause() -> None:
    with pytest.raises(FeedParseError) as caught:
        parse_feed(b"<<<definitely not xml", "https://e.com/f")
    assert caught.value.__cause__ is not None or "not an RSS" in str(caught.value)


def test_invalid_json_chains_its_cause() -> None:
    with pytest.raises(FeedParseError) as caught:
        parse_feed(b"{oops", "https://e.com/f")
    assert isinstance(caught.value.__cause__, ValueError)


def test_parse_only_trims_to_newest_before_building_items() -> None:
    feed = parse_feed(_many_items(MAX_ITEMS + 5), "https://e.com/feed")
    assert [item.guid for item in feed.items[:2]] == [f"g{MAX_ITEMS + 4}", f"g{MAX_ITEMS + 3}"]


def _many_items(count: int) -> bytes:
    entries = "".join(
        f"<item><title>t{n}</title><link>https://e.com/{n}</link><guid>g{n}</guid>"
        f"<pubDate>Mon, 12 Oct 2026 {n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d} GMT</pubDate>"
        "</item>"
        for n in range(count)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        f"<link>https://e.com/</link>{entries}</channel></rss>"
    ).encode()


def test_file_url_body_is_not_read_or_fetched() -> None:
    with pytest.raises(FeedParseError):
        parse_feed(b"file:///etc/hosts", "https://e.com/feed")


def test_external_entity_is_ignored() -> None:
    body = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/hosts">]>'
        b'<rss version="2.0"><channel><title>T &x;</title><link>https://e.com/</link>'
        b"<item><title>a &x;</title><link>https://e.com/a</link></item></channel></rss>"
    )
    feed = parse_feed(body, "https://e.com/feed")
    assert "localhost" not in feed.title and "localhost" not in feed.items[0].title
