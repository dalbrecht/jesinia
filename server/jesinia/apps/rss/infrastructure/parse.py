# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse RSS/Atom (via ``feedparser``) and JSON Feed into ``ParsedFeed``; discover feed links."""

from __future__ import annotations

import calendar
import html as html_entities
import json
import re
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import nh3

from jesinia.apps.rss.domain.feed import (
    FeedParseError,
    ParsedFeed,
    ParsedItem,
    has_credentials,
    strip_credentials,
)
from jesinia.services.fetch.api.content import sanitize_html

MAX_ITEMS = 500
SNIPPET_MAX = 280
HN_HOST = "news.ycombinator.com"
FEED_LINK_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/feed+json"}
)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class _RawEntry:
    """Format-neutral entry: both adapters produce this, ``_build_item`` consumes it."""

    guid: str | None
    link: str | None
    title: str | None
    author: str | None
    published_at: datetime | None
    summary_html: str | None
    content_html: str | None
    comments_url: str | None


@dataclass(frozen=True)
class _RawFeed:
    title: str | None
    site_url: str | None
    entries: list[_RawEntry]
    warning: str | None = None
    cause: BaseException | None = None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _safe_url(candidate: str | None, base: str) -> str | None:
    """Resolve against ``base``; keep only absolute http(s) URLs with a host."""
    if not candidate:
        return None
    try:
        resolved = urljoin(base, candidate)
        parts = urlparse(resolved)
        has_host = bool(parts.hostname)
    except ValueError:
        return None
    return resolved if parts.scheme in ("http", "https") and has_host else None


def _public_url(candidate: str | None, base: str) -> str | None:
    """``_safe_url`` without credentials: item and site URLs end up in kept pages (git)."""
    url = _safe_url(candidate, base)
    return url if url is not None and not has_credentials(url) else None


def _plain_text(html: str) -> str:
    stripped = html_entities.unescape(nh3.clean(html, tags=set()))
    return _WHITESPACE.sub(" ", stripped).strip()


def _snippet(html: str | None) -> str:
    text = _plain_text(html) if html else ""
    return text if len(text) <= SNIPPET_MAX else text[: SNIPPET_MAX - 1].rstrip() + "…"


def _read_feedparser(body: bytes) -> _RawFeed:
    # feedparser ships no type information; everything it returns is confined to this adapter.
    parsed: Any = feedparser.parse(body)
    feed: Any = parsed.feed
    entries: list[_RawEntry] = []
    for entry in parsed.entries:
        with warnings.catch_warnings():
            # feedparser warns about its own updated->published alias when a date is missing.
            warnings.simplefilter("ignore", DeprecationWarning)
            moment = entry.get("published_parsed") or entry.get("updated_parsed")
        contents = entry.get("content") or []
        entries.append(
            _RawEntry(
                guid=_text(entry.get("id")),
                link=_text(entry.get("link")),
                title=_text(entry.get("title")),
                author=_text(entry.get("author")),
                published_at=(
                    datetime.fromtimestamp(calendar.timegm(moment), tz=UTC) if moment else None
                ),
                summary_html=_text(entry.get("summary")),
                content_html=_text(contents[0].get("value")) if contents else None,
                comments_url=_text(entry.get("comments")),
            )
        )
    cause = parsed.get("bozo_exception") if parsed.get("bozo") else None
    warning = f"{type(cause).__name__}: {cause}" if cause is not None else None
    if warning is None and parsed.get("bozo"):
        warning = "feed was malformed"
    return _RawFeed(_text(feed.get("title")), _text(feed.get("link")), entries, warning, cause)


def _json_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _json_author(item: dict[str, Any]) -> str | None:
    authors = item.get("authors")
    candidates = authors if isinstance(authors, list) else [item.get("author")]
    for candidate in candidates:
        if isinstance(candidate, dict) and (name := _text(candidate.get("name"))):
            return name
    return None


def _read_json_feed(body: bytes) -> _RawFeed:
    try:
        document = json.loads(body)
    except ValueError as error:
        raise FeedParseError("not valid JSON Feed") from error
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise FeedParseError("JSON document has no items list")
    entries = []
    for item in document["items"]:
        if not isinstance(item, dict):
            continue
        content_html = _text(item.get("content_html"))
        content_text = _text(item.get("content_text"))
        if content_html is None and content_text is not None:
            content_html = f"<p>{html_entities.escape(content_text)}</p>"
        entries.append(
            _RawEntry(
                guid=_text(item.get("id")),
                link=_text(item.get("url")),
                title=_text(item.get("title")),
                author=_json_author(item),
                published_at=_json_date(item.get("date_published")),
                summary_html=_text(item.get("summary")),
                content_html=content_html,
                comments_url=None,
            )
        )
    return _RawFeed(_text(document.get("title")), _text(document.get("home_page_url")), entries)


def _sort_key(entry: _RawEntry) -> datetime:
    return entry.published_at or datetime.min.replace(tzinfo=UTC)


def _build_item(raw: _RawEntry, feed_url: str, is_hacker_news: bool) -> ParsedItem:
    url = _public_url(raw.link, feed_url) or feed_url
    title = raw.title or ""
    sanitised = sanitize_html(raw.content_html, url) if raw.content_html else None
    return ParsedItem(
        guid=raw.guid or url + title,
        url=url,
        title=title,
        author=raw.author,
        published_at=raw.published_at,
        snippet=_snippet(raw.summary_html or raw.content_html),
        content_html=sanitised,
        discussion_url=_public_url(raw.comments_url, feed_url) if is_hacker_news else None,
        is_summary=raw.content_html is None,
    )


def parse_feed(body: bytes, url: str) -> ParsedFeed:
    """Parse ``body`` fetched from ``url``; keep the newest ``MAX_ITEMS`` entries.

    Only ever handed bytes: nothing here reads files or fetches network resources. Raises
    ``FeedParseError`` (chained to the parser's own cause) when the document is unreadable.
    """
    is_json = body.lstrip()[:1] in (b"{", b"[")
    raw = _read_json_feed(body) if is_json else _read_feedparser(body)
    if not is_json and not raw.entries and raw.title is None:
        raise FeedParseError(raw.warning or "document is not an RSS or Atom feed") from raw.cause
    # A redirect may have landed on a URL with userinfo: it is the fallback for item and
    # site links, which end up in kept pages, so never with credentials in it.
    url = strip_credentials(url)
    is_hacker_news = urlparse(url).hostname == HN_HOST
    # Stable sort, so undated feeds keep their own order; trim before the costly per-item work.
    newest = sorted(raw.entries, key=_sort_key, reverse=True)[:MAX_ITEMS]
    return ParsedFeed(
        title=raw.title or url,
        site_url=_public_url(raw.site_url, url) or url,
        items=[_build_item(entry, url, is_hacker_news) for entry in newest],
        warning=raw.warning,
    )


class _FeedLinkFinder(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__()
        self._base = base
        self.found: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link" or self.found is not None:
            return
        values = {name: (value or "") for name, value in attrs}
        is_alternate = "alternate" in values.get("rel", "").lower().split()
        if (
            is_alternate
            and values.get("type", "").lower() in FEED_LINK_TYPES
            and values.get("href")
        ):
            self.found = _safe_url(values["href"], self._base)


def discover(html: bytes, base: str) -> str | None:
    """Return the first ``<link rel=alternate>`` feed URL in ``html``, resolved against ``base``."""
    finder = _FeedLinkFinder(base)
    finder.feed(html.decode("utf-8", errors="replace"))
    return finder.found


class FeedparserReader:
    """``FeedReader`` backed by ``feedparser`` and the JSON Feed adapter."""

    def discover(self, html: bytes, base: str) -> str | None:
        return discover(html, base)

    def parse(self, body: bytes, url: str) -> ParsedFeed:
        return parse_feed(body, url)
