# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the fetch service needs from the outside world."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True)
class CachedFetch:
    etag: str | None
    last_modified: str | None
    status: int
    content_type: str
    body: bytes


class FetchCache(Protocol):
    def get(self, url: str) -> CachedFetch | None: ...

    def put(self, url: str, entry: CachedFetch) -> None: ...

    def prune(self, older_than: timedelta) -> int: ...


@dataclass(frozen=True)
class Article:
    """A page's main text as Markdown, from HTML that passed the allowlist sanitiser first.

    Guaranteed: every link and image target the extractor emits, and every entry of
    ``image_urls`` (the article's images in order, no repeats), is an absolute http(s) URL;
    scripts and styles are gone. Not guaranteed: page text that merely looks like Markdown
    is escaped by the extractor, but the result is still untrusted and must be rendered by
    a sanitising Markdown renderer.
    """

    title: str | None
    markdown: str
    image_urls: list[str]


class ArticleExtractor(Protocol):
    def extract(self, html: bytes, url: str) -> Article | None:
        """The article in ``html`` (fetched from ``url``), or None if it has no main text."""
        ...
