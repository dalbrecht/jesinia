# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polling: read each due feed, store its new posts and adapt when it is read next.

Every request goes through the ``fetch`` service (SSRF guard, size cap, pacing, and
conditional requests through its own cache). A result served from that cache means the
feed has not changed since it was last read. One feed's failure is recorded on that feed,
with its cause, and never stops the others; a failing feed is retried with backoff and
never dropped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from jesinia.apps.rss.application.ports import FeedFetcher, FeedReader, FeedStore, PollState
from jesinia.apps.rss.domain.feed import (
    Feed,
    FeedNotFound,
    failure_delay_s,
    host_of,
    next_interval_s,
    redact_urls,
)
from jesinia.services.fetch.application.service import FetchResult

log = logging.getLogger(__name__)

# At most this many feed requests at once, and one at a time per host (spec §4.2).
MAX_IN_FLIGHT = 8


@dataclass(frozen=True)
class PollSummary:
    """What one round did: feeds polled, posts that were new, and feeds that failed."""

    polled: int
    new_items: int
    failed: int


@dataclass(frozen=True)
class _Outcome:
    new_items: int
    failed: bool


def _cause(exc: Exception) -> str:
    """The error's type and message, as a feed's ``last_error`` (some messages are empty)."""
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class Poller:
    def __init__(
        self,
        store: FeedStore,
        fetcher: FeedFetcher,
        reader: FeedReader,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._reader = reader
        self._clock = clock
        self._in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._hosts: dict[str, asyncio.Semaphore] = {}
        # A scheduled round and a manual Refresh never run at once.
        self._round = asyncio.Lock()
        # One poll of a feed at a time: a feed's Retry waits only for that feed, never for a
        # whole round (hundreds of feeds, one request per host).
        self._feed_locks: dict[int, asyncio.Lock] = {}

    async def poll_due(self) -> PollSummary:
        """Poll the feeds whose next poll time has come (the scheduled tick)."""
        async with self._round:
            now = self._clock()
            return await self._poll(f for f in self._store.feeds() if f.next_poll_at <= now)

    async def poll_all(self) -> PollSummary:
        """Poll every feed now (the manual Refresh)."""
        async with self._round:
            return await self._poll(self._store.feeds())

    async def poll_feed(self, feed_id: int) -> None:
        """Poll one feed now (a feed's Retry); its outcome is recorded on the feed.

        Raises ``FeedNotFound`` for an unknown or unsubscribed feed.
        """
        feed = self._store.feed(feed_id)
        if feed is None or feed.deleted:
            raise FeedNotFound(f"no feed {feed_id}")
        await self._poll([feed])

    async def _poll(self, feeds: Iterable[Feed]) -> PollSummary:
        due = list(feeds)
        results = await asyncio.gather(*(self._poll_one(f) for f in due), return_exceptions=True)
        outcomes: list[_Outcome] = []
        errors: list[BaseException] = []
        for feed, result in zip(due, results, strict=True):
            # Only recording an outcome can raise (the database itself failed): log each
            # with its cause, then surface the first once every feed has finished.
            if isinstance(result, BaseException):
                log.error(
                    "rss poll of feed %d (%s) could not be recorded: %s",
                    feed.id,
                    host_of(feed.url),
                    redact_urls(str(result)),
                    exc_info=result,
                )
                errors.append(result)
            else:
                outcomes.append(result)
        if errors:
            raise errors[0]
        return PollSummary(
            polled=len(due),
            new_items=sum(o.new_items for o in outcomes),
            failed=sum(o.failed for o in outcomes),
        )

    async def _fetch(self, url: str) -> FetchResult:
        host = urlsplit(url).hostname or url
        # Host first, then a global slot: a queue for one busy host never holds slots
        # other hosts could use. fetch's own pacing only spaces request starts.
        async with self._hosts.setdefault(host, asyncio.Semaphore(1)), self._in_flight:
            return await self._fetcher.get(url)

    async def _poll_one(self, queued: Feed) -> _Outcome:
        async with self._feed_locks.setdefault(queued.id, asyncio.Lock()):
            # Read again: a Retry may have polled it (and changed its state) meanwhile.
            feed = self._store.feed(queued.id)
            if feed is None or feed.deleted:
                return _Outcome(0, failed=False)
            return await self._poll_locked(feed)

    async def _poll_locked(self, feed: Feed) -> _Outcome:
        try:
            result = await self._fetch(feed.url)
            if result.status != 200:
                return self._failed(feed, f"HTTP {result.status}")
            # fetch caches any 200 with validators, readable or not: a cached body counts
            # as "unchanged" only after a poll that read it fine. Otherwise it is parsed
            # again, so an unreadable feed keeps failing (and reaches its warning).
            # Tombstones are left alone: the last full read refreshed the ones it offered.
            if result.from_cache and feed.fail_count == 0 and feed.last_ok_at is not None:
                return self._succeeded(feed, 0, feed.last_error)  # a recovered parse's warning
            # feedparser and the sanitiser take up to seconds on a big feed: off the loop.
            parsed = await asyncio.to_thread(self._reader.parse, result.body, result.url)
            new_items = self._store.add_posts(feed.id, parsed.items, self._clock())
        # Isolation boundary: whatever one feed raises (fetch, parse or a store error for
        # that feed) is recorded on that feed with its cause, and the round goes on.
        except Exception as exc:
            log.warning(
                "rss poll of feed %d (%s) failed: %s",
                feed.id,
                host_of(feed.url),
                redact_urls(_cause(exc)),
            )
            return self._failed(feed, _cause(exc))
        return self._succeeded(feed, new_items, parsed.warning)

    def _succeeded(self, feed: Feed, new_items: int, warning: str | None) -> _Outcome:
        now = self._clock()
        interval = next_interval_s(feed.interval_s, has_new_items=new_items > 0)
        state = PollState(interval, now + timedelta(seconds=interval), 0, warning, now)
        self._store.set_poll_state(feed.id, state)
        return _Outcome(new_items, failed=False)

    def _failed(self, feed: Feed, cause: str) -> _Outcome:
        now = self._clock()
        fails = feed.fail_count + 1
        delay = timedelta(seconds=failure_delay_s(fails))
        # The interval is kept, so a feed that recovers adapts from where it was.
        state = PollState(feed.interval_s, now + delay, fails, cause, feed.last_ok_at)
        self._store.set_poll_state(feed.id, state)
        return _Outcome(0, failed=True)
