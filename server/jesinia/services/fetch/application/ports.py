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
    """A page's main text as Markdown. Links and images are absolute http(s) URLs;
    ``image_urls`` lists the images in the order they appear."""

    title: str | None
    markdown: str
    image_urls: list[str]


class ArticleExtractor(Protocol):
    def extract(self, html: bytes, url: str) -> Article | None:
        """The article in ``html`` (fetched from ``url``), or None if it has no main text."""
        ...
