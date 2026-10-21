# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports the RSS use cases depend on; infrastructure supplies the implementations."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jesinia.apps.rss.domain.feed import Feed, FeedView, Folder, ParsedFeed, ParsedItem
from jesinia.apps.rss.domain.kept import ArchiveState
from jesinia.apps.rss.domain.post import Counts, Cursor, PostView, View
from jesinia.apps.rss.domain.settings import Retention
from jesinia.services.fetch.application.ports import Article
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


class ArticleFetcher(Protocol):
    """The ``fetch`` service's article extraction (a guarded GET, then the extractor)."""

    async def article(self, url: str) -> Article | None:
        """The page's main text, or None if it has none; raises ``FetchError``."""
        ...


class ArticleConverter(Protocol):
    """The ``fetch`` service's extractor applied to HTML already in hand (no request)."""

    async def convert(self, html: bytes, url: str) -> Article | None:
        """``html`` as Markdown, sanitised like a fetched article; raises ``FetchError``."""
        ...


class ImageFetcher(Protocol):
    """The ``fetch`` service's guarded GET (SSRF checks on every hop), size-capped."""

    async def get(self, url: str, *, max_bytes: int, cache: bool) -> FetchResult:
        """Fetch ``url`` (``cache=False``: no revalidation, nothing stored); raises
        ``FetchError`` when it is blocked or over ``max_bytes``."""
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

    def atomic(self) -> AbstractContextManager[None]:
        """A transaction: the calls inside it all happen, or none do."""
        ...

    def feed(self, feed_id: int) -> Feed | None: ...

    def feed_by_url(self, url: str) -> Feed | None: ...

    def feeds(self) -> list[Feed]:
        """Every subscribed (not deleted) feed with its polling state."""
        ...

    def set_poll_state(self, feed_id: int, state: PollState) -> None:
        """Record a poll's outcome (a feed deleted meanwhile is left deleted)."""
        ...

    def insert_feed(self, feed: NewFeed) -> Feed:
        """Insert a feed whose URL is not subscribed yet (callers check ``feed_by_url``), or
        restore the deleted feed with that URL."""
        ...

    def delete_feed(self, feed_id: int, now: datetime) -> bool:
        """Unsubscribe: hide the feed and delete its posts except kept ones (and ones being
        kept), which stay, read; False if there was no such subscribed feed."""
        ...

    def set_feed_folder(self, feed_id: int, folder_id: int | None) -> bool:
        """Move the feed; False if there was no such feed."""
        ...

    def set_feed_retention(self, feed_id: int, retention: Retention | None) -> bool:
        """Set (or clear, with None) the feed's own retention; False if there was no such
        subscribed feed."""
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


class PostStore(Protocol):
    """Posts and their read state in ``rss.sqlite``. Lists are newest first: by publication
    time, else first sighting, then by id."""

    def posts(
        self,
        view: View,
        feed_id: int | None,
        folder_id: int | None,
        before: Cursor | None,
        limit: int,
    ) -> list[PostView]:
        """Up to ``limit`` posts in ``view`` (and the feed or folder), after ``before``."""
        ...

    def post(self, post_id: int) -> PostView | None: ...

    def full_md(self, post_id: int) -> str | None:
        """The cached full article, if it was loaded."""
        ...

    def set_full_md(self, post_id: int, markdown: str) -> None: ...

    def set_read(self, ids: Sequence[int], read: bool) -> int:
        """Set the read state of the posts that exist; how many there were."""
        ...

    def mark_unread_read(
        self, feed_id: int | None, folder_id: int | None, as_of: datetime
    ) -> list[int]:
        """Mark read every unread post within the feed and folder (if given) that was first
        seen by ``as_of``; their ids, ascending."""
        ...

    def in_scope(
        self, post_id: int, view: View, feed_id: int | None, folder_id: int | None
    ) -> bool:
        """Whether the post is in the view's list (within the feed and folder, if given)."""
        ...

    def mark_read_from(
        self,
        view: View,
        feed_id: int | None,
        folder_id: int | None,
        start: Cursor,
        as_of: datetime,
    ) -> int:
        """Mark read every post in the view at or above ``start`` that was first seen by
        ``as_of``; how many matched."""
        ...

    def search(self, text: str, limit: int) -> list[PostView]:
        """Posts whose title or snippet contains ``text`` (literally, ignoring ASCII case)."""
        ...

    def prune(self, cutoff: datetime, now: datetime) -> int:
        """Delete posts dated before ``cutoff`` that are not kept nor being kept
        (remembering their GUIDs, so the feed cannot bring them back); how many."""
        ...

    def counts(self) -> Counts: ...

    def set_kept(self, post_id: int, page_id: str, state: ArchiveState) -> None:
        """Record the KB page the post was kept to and its archive state, together."""
        ...

    def set_post_archive_state(
        self, post_id: int, state: ArchiveState | None, error: str | None
    ) -> None:
        """Set one post's archive state (before its page exists, or to undo that)."""
        ...

    def page_archive_state(self, page_id: str) -> ArchiveState | None:
        """How far the page's archive got, by any post kept to it: ``done`` if one finished,
        else ``archiving``, else ``failed``; None when no post here knows the page."""
        ...

    def archive_states_of_page(
        self, page_id: str
    ) -> list[tuple[int, ArchiveState | None, str | None]]:
        """``(post id, archive state, error)`` for every post kept to the page."""
        ...

    def set_archive_state(self, page_id: str, state: ArchiveState, error: str | None) -> None:
        """Set the archive state (and error) of every post kept to the page."""
        ...

    def archiving(self) -> list[tuple[str | None, int]]:
        """``(page id, post id)`` for each page whose archive was left running, one post
        per page; the page id is None for a post marked before its page was written."""
        ...
