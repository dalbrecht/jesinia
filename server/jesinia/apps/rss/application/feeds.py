# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscriptions: add (with autodiscovery), remove, move, folders, and OPML in and out."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jesinia.apps.rss.application.ports import FeedFetcher, FeedReader, FeedStore, NewFeed
from jesinia.apps.rss.domain.feed import (
    FIRST_INTERVAL_S,
    FOLDER_NAME_MAX,
    Feed,
    FeedExists,
    FeedNotFound,
    FeedParseError,
    FeedUnavailable,
    FeedView,
    Folder,
    FolderExists,
    FolderNotFound,
    InvalidFeedUrl,
    ParsedFeed,
    validate_feed_title,
    validate_feed_url,
    validate_folder_name,
)
from jesinia.apps.rss.domain.opml import OpmlFeed, parse_opml, render_opml
from jesinia.apps.rss.domain.settings import Retention
from jesinia.services.fetch.application.service import FetchError, FetchResult


@dataclass(frozen=True)
class ImportFailure:
    url: str
    cause: str


@dataclass(frozen=True)
class ImportResult:
    """``existing`` counts feeds already subscribed, including repeats within the file."""

    added: int
    existing: int
    failed: list[ImportFailure]


def _is_html(result: FetchResult) -> bool:
    return "html" in result.content_type.lower()


class FeedService:
    def __init__(
        self,
        store: FeedStore,
        fetcher: FeedFetcher,
        reader: FeedReader,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._reader = reader
        self._now = now

    def _subscribed(self, url: str) -> bool:
        found = self._store.feed_by_url(url)
        return found is not None and not found.deleted

    def _refuse_existing(self, url: str) -> None:
        if self._subscribed(url):
            raise FeedExists(f"already subscribed to {url}", url)

    async def _fetch(self, url: str) -> FetchResult:
        try:
            result = await self._fetcher.get(url)
        except FetchError as exc:
            raise FeedUnavailable(f"could not fetch {url}: {exc}") from exc
        if result.status != 200:
            raise FeedUnavailable(f"{url} answered HTTP {result.status}")
        return result

    async def _parse(self, result: FetchResult) -> ParsedFeed:
        # feedparser and the sanitiser take up to seconds on a big feed: off the event loop.
        return await asyncio.to_thread(self._reader.parse, result.body, result.url)

    async def _load(self, url: str) -> tuple[str, ParsedFeed]:
        """The feed URL to subscribe to and its parsed content, discovering it on a page."""
        result = await self._fetch(url)
        parse_error: FeedParseError | None = None
        if not _is_html(result):
            try:
                return url, await self._parse(result)
            except FeedParseError as exc:
                parse_error = exc  # maybe a mislabelled page: look for a feed link next
        found = self._reader.discover(result.body, result.url)
        if found is None:
            if parse_error is not None:
                raise FeedUnavailable(
                    f"{url} is not a readable feed: {parse_error}"
                ) from parse_error
            try:
                # Feeds are often mislabelled text/html: with no feed link, read it as one.
                return url, await self._parse(result)
            except FeedParseError as exc:
                raise FeedUnavailable(
                    f"no feed found at {url}: the page links to none and is not a feed ({exc})"
                ) from exc
        found = validate_feed_url(found)  # e.g. a link with credentials in it
        self._refuse_existing(found)
        linked = await self._fetch(found)
        try:
            return found, await self._parse(linked)
        except FeedParseError as exc:
            raise FeedUnavailable(f"{found} is not a readable feed: {exc}") from exc

    async def add(self, url: str, folder: str | None) -> Feed:
        """Subscribe to a feed or a site's advertised feed, keeping its current items.

        Raises ``InvalidFeedUrl``, ``InvalidFolderName``, ``FeedExists`` or
        ``FeedUnavailable`` (with the fetch or parse cause).
        """
        url = validate_feed_url(url)
        folder_name = validate_folder_name(folder) if folder is not None else None
        self._refuse_existing(url)
        feed_url, parsed = await self._load(url)
        # Checked again: another add may have subscribed while this one was fetching.
        self._refuse_existing(feed_url)
        now = self._now()
        folder_id = self._store.ensure_folder(folder_name).id if folder_name else None
        feed = self._store.insert_feed(
            NewFeed(
                url=feed_url,
                title=parsed.title,
                site_url=parsed.site_url,
                folder_id=folder_id,
                interval_s=FIRST_INTERVAL_S,
                next_poll_at=now + timedelta(seconds=FIRST_INTERVAL_S),
                last_ok_at=now,
                last_error=parsed.warning,  # a recovered parse keeps its cause on record
            )
        )
        self._store.add_posts(feed.id, parsed.items, now)
        return feed

    def view(self, feed_id: int) -> FeedView:
        for found in self._store.feed_views():
            if found.id == feed_id:
                return found
        raise FeedNotFound(f"no feed {feed_id}")

    def remove(self, feed_id: int) -> None:
        """Unsubscribe: the feed and its posts go, except kept posts, which stay in the Kept
        view (their pages are in the KB). Subscribing again restores the feed."""
        if not self._store.delete_feed(feed_id, self._now()):
            raise FeedNotFound(f"no feed {feed_id}")
        self._store.drop_empty_folders()

    def move(self, feed_id: int, folder: str | None) -> None:
        """Put the feed in ``folder`` (created if new), or in no folder when None."""
        name = validate_folder_name(folder) if folder is not None else None
        found = self._store.feed(feed_id)
        if found is None or found.deleted:
            raise FeedNotFound(f"no feed {feed_id}")
        folder_id = self._store.ensure_folder(name).id if name else None
        self._store.set_feed_folder(feed_id, folder_id)
        self._store.drop_empty_folders()

    def feed(self, feed_id: int) -> Feed:
        """A subscribed feed with its polling state; ``FeedNotFound`` otherwise."""
        found = self._store.feed(feed_id)
        if found is None or found.deleted:
            raise FeedNotFound(f"no feed {feed_id}")
        return found

    def rename(self, feed_id: int, title: str) -> FeedView:
        """Retitle the feed. Raises ``InvalidFeedTitle`` or ``FeedNotFound``."""
        if not self._store.rename_feed(feed_id, validate_feed_title(title)):
            raise FeedNotFound(f"no feed {feed_id}")
        return self.view(feed_id)

    def delete_folder(self, folder_id: int) -> int:
        """Delete the folder; its feeds stay subscribed, in no folder. How many it held.
        Raises ``FolderNotFound``."""
        if self._store.folder(folder_id) is None:
            raise FolderNotFound(f"no folder {folder_id}")
        return self._store.unfile_folder(folder_id)

    def set_retention(self, feed_id: int, retention: Retention | None) -> FeedView:
        """Give the feed its own retention, or None to follow the global one."""
        if not self._store.set_feed_retention(feed_id, retention):
            raise FeedNotFound(f"no feed {feed_id}")
        return self.view(feed_id)

    def rename_folder(self, folder_id: int, name: str) -> Folder:
        new_name = validate_folder_name(name)
        if self._store.folder(folder_id) is None:
            raise FolderNotFound(f"no folder {folder_id}")
        other = self._store.folder_by_name(new_name)
        if other is not None and other.id != folder_id:
            raise FolderExists(f"a folder named {other.name} already exists")
        return self._store.rename_folder(folder_id, new_name)

    async def import_opml(self, xml: str) -> ImportResult:
        """Subscribe to every new feed in an OPML document, keeping one folder level, all
        or nothing.

        Nothing is fetched here: imported feeds are due at once, so the poller reads them
        (and reports a failing one on the feed) without holding up the upload. Raises
        ``InvalidOpml``; a feed whose URL cannot be used is listed in ``failed``.
        """
        entries = await asyncio.to_thread(parse_opml, xml)  # up to 5 MB of XML
        with self._store.atomic():
            return self._import(entries)

    def _import(self, entries: builtins.list[OpmlFeed]) -> ImportResult:
        added, existing = 0, 0
        failed: builtins.list[ImportFailure] = []
        now = self._now()
        for entry in entries:
            try:
                url = validate_feed_url(entry.url)
            except InvalidFeedUrl as exc:
                failed.append(ImportFailure(entry.url, str(exc)))
                continue
            if self._subscribed(url):
                existing += 1
                continue
            folder_id = None
            if entry.folder is not None:
                name = validate_folder_name(entry.folder[:FOLDER_NAME_MAX])
                folder_id = self._store.ensure_folder(name).id
            self._store.insert_feed(
                NewFeed(url, entry.title or url, None, folder_id, FIRST_INTERVAL_S, now, None, None)
            )
            added += 1
        return ImportResult(added, existing, failed)

    def export_opml(self) -> str:
        return render_opml(OpmlFeed(v.url, v.title, v.folder) for v in self._store.feed_views())

    def list(self) -> builtins.list[FeedView]:
        return self._store.feed_views()
