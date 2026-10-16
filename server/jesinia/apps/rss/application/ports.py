# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports the RSS use cases depend on; infrastructure supplies the implementations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jesinia.apps.rss.domain.feed import Feed, FeedView, Folder, ParsedFeed, ParsedItem
from jesinia.services.fetch.application.service import FetchResult


class FeedReader(Protocol):
    def discover(self, html: bytes, base: str) -> str | None:
        """Return the absolute URL of the feed an HTML page advertises, if any."""
        ...

    def parse(self, body: bytes, url: str) -> ParsedFeed:
        """Parse an RSS, Atom or JSON Feed document; raise ``FeedParseError`` if unreadable."""
        ...


class FeedFetcher(Protocol):
    """The foundation ``fetch`` service: guarded (SSRF), paced and size-capped GETs."""

    async def get(self, url: str) -> FetchResult:
        """Fetch ``url``; raises ``FetchError`` when the URL is blocked or too large."""
        ...


@dataclass(frozen=True)
class NewFeed:
    url: str
    title: str
    site_url: str | None
    folder_id: int | None
    interval_s: int
    next_poll_at: datetime
    last_ok_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class PollState:
    """A feed's polling state after a poll: its schedule and the outcome."""

    interval_s: int
    next_poll_at: datetime
    fail_count: int
    last_error: str | None
    last_ok_at: datetime | None


class FeedStore(Protocol):
    """Feeds, folders and posts in ``rss.sqlite``."""

    def feed(self, feed_id: int) -> Feed | None: ...

    def feed_by_url(self, url: str) -> Feed | None: ...

    def feeds(self) -> list[Feed]:
        """Every subscribed feed with its polling state."""
        ...

    def set_poll_state(self, feed_id: int, state: PollState) -> None:
        """Record a poll's outcome (a feed deleted meanwhile is left deleted)."""
        ...

    def insert_feed(self, feed: NewFeed) -> Feed:
        """Insert a feed whose URL is not subscribed yet (callers check ``feed_by_url``)."""
        ...

    def delete_feed(self, feed_id: int) -> bool:
        """Delete the feed and its posts; False if there was no such feed."""
        ...

    def set_feed_folder(self, feed_id: int, folder_id: int | None) -> bool:
        """Move the feed; False if there was no such feed."""
        ...

    def add_posts(self, feed_id: int, items: Sequence[ParsedItem], now: datetime) -> int:
        """Insert items not seen before (by feed and GUID); how many were new."""
        ...

    def folder(self, folder_id: int) -> Folder | None: ...

    def folder_by_name(self, name: str) -> Folder | None: ...

    def ensure_folder(self, name: str) -> Folder:
        """The folder called ``name``, created at the end of the list if it is new."""
        ...

    def rename_folder(self, folder_id: int, name: str) -> Folder: ...

    def drop_empty_folders(self) -> int:
        """Delete folders no feed is in (folders exist only to group feeds)."""
        ...

    def feed_views(self) -> list[FeedView]:
        """Every feed with its folder name and unread count, in folder then title order."""
        ...
