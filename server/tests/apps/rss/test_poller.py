# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import respx

from jesinia.apps.rss.application.feeds import FeedService
from jesinia.apps.rss.application.poller import Poller, PollSummary
from jesinia.apps.rss.application.ports import FeedFetcher, NewFeed, PollState
from jesinia.apps.rss.domain.feed import (
    FIRST_INTERVAL_S,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    Feed,
    ParsedItem,
    failure_delay_s,
    next_interval_s,
)
from jesinia.apps.rss.infrastructure.parse import MAX_ITEMS, FeedparserReader
from jesinia.apps.rss.infrastructure.store import SqliteFeedStore, SqlitePostStore
from jesinia.apps.rss.module import RSS_DB
from jesinia.core.db import open_db
from jesinia.services.fetch.application.ports import CachedFetch
from jesinia.services.fetch.application.service import (
    FetchError,
    FetchResult,
    FetchService,
)

START = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
PUBLIC = "93.184.216.34"


def _rss(*guids: str) -> bytes:
    items = "".join(
        f"<item><title>{g}</title><link>https://example.com/{g}</link><guid>{g}</guid></item>"
        for g in guids
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        f"<link>https://example.com/</link>{items}</channel></rss>"
    ).encode()


def _ok(url: str, body: bytes, *, from_cache: bool = False) -> FetchResult:
    return FetchResult(url, 200, "application/rss+xml", body, from_cache)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeFetcher:
    """Answers from ``responses`` (a result or an exception to raise), tracking overlap."""

    responses: dict[str, FetchResult | Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    delay: float = 0.0
    in_flight: dict[str, int] = field(default_factory=dict)
    max_per_host: int = 0
    total: int = 0
    max_total: int = 0

    async def get(self, url: str) -> FetchResult:
        host = urlsplit(url).hostname or ""
        self.calls.append(url)
        self.in_flight[host] = self.in_flight.get(host, 0) + 1
        self.total += 1
        self.max_per_host = max(self.max_per_host, self.in_flight[host])
        self.max_total = max(self.max_total, self.total)
        try:
            await asyncio.sleep(self.delay)
            answer = self.responses[url]
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self.in_flight[host] -= 1
            self.total -= 1


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, RSS_DB)
    yield connection
    connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SqliteFeedStore:
    return SqliteFeedStore(conn)


def _subscribe(store: SqliteFeedStore, url: str, due: datetime = START) -> Feed:
    return store.insert_feed(NewFeed(url, url, None, None, FIRST_INTERVAL_S, due, None, None))


def _poller(store: SqliteFeedStore, fetcher: FeedFetcher, clock: Clock) -> Poller:
    return Poller(store, fetcher, FeedparserReader(), clock)


def _feed(store: SqliteFeedStore, feed_id: int) -> Feed:
    found = store.feed(feed_id)
    assert found is not None
    return found


def _guids(conn: sqlite3.Connection, feed_id: int) -> list[str]:
    rows = conn.execute("SELECT guid FROM posts WHERE feed_id = ? ORDER BY guid", (feed_id,))
    return [str(row[0]) for row in rows]


def _warning(store: SqliteFeedStore, feed_id: int) -> str | None:
    return next(v.warning for v in store.feed_views() if v.id == feed_id)


# --- the schedule rules (domain) --------------------------------------------------------------


def test_interval_rules_stay_between_bounds() -> None:
    assert next_interval_s(FIRST_INTERVAL_S, has_new_items=True) == 900
    assert next_interval_s(MIN_INTERVAL_S, has_new_items=True) == MIN_INTERVAL_S
    assert next_interval_s(FIRST_INTERVAL_S, has_new_items=False) == 2700
    assert next_interval_s(40_000, has_new_items=False) == MAX_INTERVAL_S
    assert [failure_delay_s(n) for n in (1, 2, 3, 4, 5, 500)] == [
        3600,
        7200,
        14_400,
        28_800,
        MAX_INTERVAL_S,
        MAX_INTERVAL_S,
    ]


# --- polling ----------------------------------------------------------------------------------


async def test_interval_adapts_between_bounds(store: SqliteFeedStore) -> None:
    clock, fetcher = Clock(), FakeFetcher()
    url = "https://a.example/feed"
    feed = _subscribe(store, url)
    poller = _poller(store, fetcher, clock)

    fetcher.responses[url] = _ok(url, _rss("1"))
    assert await poller.poll_due() == PollSummary(polled=1, new_items=1, failed=0)
    polled = _feed(store, feed.id)
    assert polled.interval_s == 900 and polled.next_poll_at == START + timedelta(seconds=900)
    assert polled.last_ok_at == START and polled.fail_count == 0

    clock.advance(899)
    assert await poller.poll_due() == PollSummary(0, 0, 0)  # not due yet: nothing fetched
    assert len(fetcher.calls) == 1

    clock.advance(1)
    fetcher.responses[url] = _ok(url, _rss("1", "2"))
    assert await poller.poll_due() == PollSummary(1, 1, 0)
    assert _feed(store, feed.id).interval_s == MIN_INTERVAL_S  # never below 15 minutes

    intervals = []
    for _ in range(12):  # nothing new: back off by half each time, up to 12 hours
        clock.now = _feed(store, feed.id).next_poll_at
        assert await poller.poll_due() == PollSummary(1, 0, 0)
        intervals.append(_feed(store, feed.id).interval_s)
    assert intervals[:3] == [1350, 2025, 3037]
    assert intervals[-1] == MAX_INTERVAL_S and max(intervals) == MAX_INTERVAL_S

    # A 304 served from fetch's conditional cache is a successful poll with no news.
    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, b"not even parsed", from_cache=True)
    assert await poller.poll_due() == PollSummary(1, 0, 0)
    cached = _feed(store, feed.id)
    assert cached.last_ok_at == clock.now and cached.last_error is None


async def test_failures_back_off_and_warn_after_three(store: SqliteFeedStore) -> None:
    clock, fetcher = Clock(), FakeFetcher()
    url = "https://flaky.example/feed"
    feed = _subscribe(store, url)
    poller = _poller(store, fetcher, clock)
    fetcher.responses[url] = httpx.ConnectTimeout("")

    delays = []
    for attempt in range(1, 7):
        assert await poller.poll_due() == PollSummary(1, 0, 1)
        failed = _feed(store, feed.id)
        assert failed.fail_count == attempt
        assert failed.last_error == "ConnectTimeout"
        assert failed.interval_s == FIRST_INTERVAL_S  # kept for when it recovers
        delays.append((failed.next_poll_at - clock.now).total_seconds())
        assert (_warning(store, feed.id) is not None) == (attempt >= 3)
        clock.now = failed.next_poll_at
    assert delays == [3600, 7200, 14_400, 28_800, MAX_INTERVAL_S, MAX_INTERVAL_S]
    assert _warning(store, feed.id) == "ConnectTimeout"

    fetcher.responses[url] = FetchResult(url, 404, "text/html", b"gone", False)
    await poller.poll_due()
    assert _feed(store, feed.id).last_error == "HTTP 404"
    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, b"<html>not a feed</html>")
    await poller.poll_due()
    assert (_feed(store, feed.id).last_error or "").startswith("FeedParseError: ")

    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, _rss("x"))
    assert await poller.poll_due() == PollSummary(1, 1, 0)
    recovered = _feed(store, feed.id)
    assert recovered.fail_count == 0 and recovered.last_error is None
    assert recovered.last_ok_at == clock.now and _warning(store, feed.id) is None
    assert recovered.interval_s == 900  # adapts from where it was before the failures


async def test_poll_caps_items_and_isolates_a_hostile_feed(
    store: SqliteFeedStore, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    async def resolve(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "internal.example" else [PUBLIC]

    class NoCache:
        def get(self, url: str) -> CachedFetch | None:
            return None

        def put(self, url: str, entry: CachedFetch) -> None:
            return None

        def prune(self, older_than: timedelta) -> int:
            return 0

    fetch = FetchService(NoCache(), resolve=resolve, min_interval=0)
    clock = Clock()
    huge = _subscribe(store, "https://huge.example/feed")
    sneaky = _subscribe(store, "https://sneaky.example/feed")
    broken = _subscribe(store, "https://broken.example/feed")
    long = _subscribe(store, "https://long.example/feed")
    good = _subscribe(store, "https://good.example/feed")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://huge.example/feed").mock(
            return_value=httpx.Response(200, content=b"<rss>" + b"x" * 5_000_001)
        )
        mock.get("https://sneaky.example/feed").mock(
            return_value=httpx.Response(302, headers={"Location": "http://internal.example/"})
        )
        mock.get("https://broken.example/feed").mock(side_effect=httpx.ConnectError("refused"))
        many = _rss(*(f"g{n:04}" for n in range(MAX_ITEMS + 100)))
        mock.get("https://long.example/feed").mock(return_value=httpx.Response(200, content=many))
        mock.get("https://good.example/feed").mock(
            return_value=httpx.Response(200, content=_rss("a", "b"))
        )
        summary = await Poller(store, fetch, FeedparserReader(), clock).poll_due()

    assert summary == PollSummary(polled=5, new_items=MAX_ITEMS + 2, failed=3)
    assert (_feed(store, huge.id).last_error or "").startswith("FetchTooLarge: ")
    assert "limit 5000000" in (_feed(store, huge.id).last_error or "")
    assert (_feed(store, sneaky.id).last_error or "").startswith("FetchBlocked: internal.example")
    # A transport failure arrives as a FetchError (cause kept) and counts like any other.
    assert _feed(store, broken.id).last_error == "FetchError: ConnectError: refused"
    assert _feed(store, broken.id).fail_count == 1
    assert len(_guids(conn, long.id)) == MAX_ITEMS  # the parser keeps the newest 500
    assert _guids(conn, good.id) == ["a", "b"]
    assert _feed(store, good.id).fail_count == 0
    assert {f.id for f in (huge, sneaky, broken, long, good)} == {
        v.id for v in store.feed_views()
    }  # a failing feed is never dropped


async def test_an_unexpected_error_in_one_feed_is_isolated(
    conn: sqlite3.Connection, store: SqliteFeedStore
) -> None:
    class FailingStore(SqliteFeedStore):
        def add_posts(self, feed_id: int, items: Sequence[ParsedItem], now: datetime) -> int:
            if feed_id == locked.id:
                raise sqlite3.OperationalError("database is locked")
            return super().add_posts(feed_id, items, now)

    clock, fetcher = Clock(), FakeFetcher()
    busy, fine = "https://busy.example/feed", "https://fine.example/feed"
    locked, kept = _subscribe(store, busy), _subscribe(store, fine)
    fetcher.responses = {busy: _ok(busy, _rss("1")), fine: _ok(fine, _rss("2"))}
    poller = _poller(FailingStore(conn), fetcher, clock)
    assert await poller.poll_all() == PollSummary(polled=2, new_items=1, failed=1)
    assert _feed(store, locked.id).last_error == "OperationalError: database is locked"
    assert _guids(conn, kept.id) == ["2"]


async def test_new_items_dedupe_by_guid_per_feed(
    store: SqliteFeedStore, conn: sqlite3.Connection
) -> None:
    clock, fetcher = Clock(), FakeFetcher()
    one, two = "https://one.example/feed", "https://two.example/feed"
    first, second = _subscribe(store, one), _subscribe(store, two)
    fetcher.responses = {one: _ok(one, _rss("same", "a")), two: _ok(two, _rss("same"))}
    poller = _poller(store, fetcher, clock)
    assert await poller.poll_all() == PollSummary(2, 3, 0)  # one GUID in two feeds: both kept

    fetcher.responses[one] = _ok(one, _rss("same", "a", "b"))
    assert await poller.poll_all() == PollSummary(2, 1, 0)
    assert _guids(conn, first.id) == ["a", "b", "same"]
    assert _guids(conn, second.id) == ["same"]
    assert _feed(store, second.id).interval_s == 1350  # its repeat brought nothing new


async def test_refresh_polls_everything_now(store: SqliteFeedStore) -> None:
    clock, fetcher = Clock(), FakeFetcher()
    later = START + timedelta(hours=6)
    urls = [f"https://f{n}.example/feed" for n in range(3)]
    for url in urls:
        _subscribe(store, url, due=later)
        fetcher.responses[url] = _ok(url, _rss(url))
    poller = _poller(store, fetcher, clock)
    assert await poller.poll_due() == PollSummary(0, 0, 0)
    assert await poller.poll_all() == PollSummary(3, 3, 0)
    assert sorted(fetcher.calls) == urls
    assert all(f.next_poll_at == START + timedelta(seconds=900) for f in _all(store))


def _all(store: SqliteFeedStore) -> list[Feed]:
    return [_feed(store, v.id) for v in store.feed_views()]


async def test_one_request_in_flight_per_host(store: SqliteFeedStore) -> None:
    clock, fetcher = Clock(), FakeFetcher(delay=0.02)
    for path in ("a", "b", "c"):
        url = f"https://slow.example/{path}"
        _subscribe(store, url)
        fetcher.responses[url] = _ok(url, _rss(path))
    other = "https://other.example/feed"
    _subscribe(store, other)
    fetcher.responses[other] = _ok(other, _rss("o"))
    assert await _poller(store, fetcher, clock).poll_due() == PollSummary(4, 4, 0)
    assert fetcher.max_per_host == 1  # the slow host's feeds never overlap
    assert fetcher.max_total == 2  # but another host is fetched alongside


async def test_at_most_eight_requests_at_once(store: SqliteFeedStore) -> None:
    clock, fetcher = Clock(), FakeFetcher(delay=0.02)
    for n in range(12):
        url = f"https://h{n}.example/feed"
        _subscribe(store, url)
        fetcher.responses[url] = _ok(url, _rss(str(n)))
    assert await _poller(store, fetcher, clock).poll_due() == PollSummary(12, 12, 0)
    assert fetcher.max_total == 8


async def test_polls_never_overlap(store: SqliteFeedStore) -> None:
    clock, fetcher = Clock(), FakeFetcher(delay=0.02)
    url = "https://a.example/feed"
    _subscribe(store, url)
    fetcher.responses[url] = _ok(url, _rss("1"))
    poller = _poller(store, fetcher, clock)
    tick, refresh = await asyncio.gather(poller.poll_due(), poller.poll_all())
    assert tick == PollSummary(1, 1, 0) and refresh == PollSummary(1, 0, 0)
    assert fetcher.max_total == 1


async def test_a_failing_store_surfaces_its_error_after_the_round(
    conn: sqlite3.Connection, store: SqliteFeedStore
) -> None:
    class BrokenStore(SqliteFeedStore):
        def set_poll_state(self, feed_id: int, state: PollState) -> None:
            if feed_id == stuck.id:
                raise sqlite3.OperationalError("disk I/O error")
            super().set_poll_state(feed_id, state)

    clock, fetcher = Clock(), FakeFetcher()
    one, two = "https://one.example/feed", "https://two.example/feed"
    stuck, fine = _subscribe(store, one), _subscribe(store, two)
    fetcher.responses = {one: _ok(one, _rss("1")), two: _ok(two, _rss("2"))}
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        await _poller(BrokenStore(conn), fetcher, clock).poll_all()
    assert _feed(store, fine.id).last_ok_at == START  # the other feed's round completed


async def test_a_cached_body_never_read_before_is_parsed(
    store: SqliteFeedStore, conn: sqlite3.Connection
) -> None:
    # Imported feeds have never been read: a cached answer (say, from an earlier
    # subscription to the same URL) still has to fill them.
    clock, fetcher = Clock(), FakeFetcher()
    url = "https://again.example/feed"
    feed = _subscribe(store, url)
    fetcher.responses[url] = _ok(url, _rss("old"), from_cache=True)
    assert await _poller(store, fetcher, clock).poll_due() == PollSummary(1, 1, 0)
    assert _guids(conn, feed.id) == ["old"]


async def test_a_cached_unreadable_feed_keeps_failing_until_it_parses(
    store: SqliteFeedStore,
) -> None:
    # fetch caches any 200 with an ETag, readable or not; its 304s must not hide the
    # parse failure, or the feed flip-flops and never reaches the warning.
    clock, fetcher = Clock(), FakeFetcher()
    url = "https://broken.example/feed"
    feed = _subscribe(store, url)
    poller = _poller(store, fetcher, clock)
    fetcher.responses[url] = _ok(url, _rss("old"))
    assert await poller.poll_due() == PollSummary(1, 1, 0)  # read fine once
    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, b"<html>not a feed</html>")
    assert await poller.poll_due() == PollSummary(1, 0, 1)
    fetcher.responses[url] = _ok(url, b"<html>not a feed</html>", from_cache=True)
    for attempt in (2, 3):
        clock.now = _feed(store, feed.id).next_poll_at
        assert await poller.poll_due() == PollSummary(1, 0, 1)
        assert _feed(store, feed.id).fail_count == attempt
    assert (_warning(store, feed.id) or "").startswith("FeedParseError: ")

    # Once a body parses, a later cached answer is "unchanged" again (and not re-parsed).
    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, _rss("a"))
    assert await poller.poll_due() == PollSummary(1, 1, 0)
    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, b"unchanged, never parsed", from_cache=True)
    assert await poller.poll_due() == PollSummary(1, 0, 0)
    assert _feed(store, feed.id).fail_count == 0


async def test_every_store_failure_in_a_round_is_logged(
    conn: sqlite3.Connection, store: SqliteFeedStore, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenStore(SqliteFeedStore):
        def set_poll_state(self, feed_id: int, state: PollState) -> None:
            raise sqlite3.OperationalError(f"disk I/O error on {feed_id}")

    clock, fetcher = Clock(), FakeFetcher()
    one, two = "https://one.example/feed", "https://two.example/feed"
    first, second = _subscribe(store, one), _subscribe(store, two)
    fetcher.responses = {one: _ok(one, _rss("1")), two: _ok(two, _rss("2"))}
    with pytest.raises(sqlite3.OperationalError):
        await _poller(BrokenStore(conn), fetcher, clock).poll_all()
    logged = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(f"disk I/O error on {first.id}" in m for m in logged)
    assert any(f"disk I/O error on {second.id}" in m for m in logged)


async def test_an_unchanged_feed_keeps_its_pruned_items_pruned(
    store: SqliteFeedStore, conn: sqlite3.Connection
) -> None:
    clock = Clock()
    url = "https://example.com/feed.xml"
    feed = _subscribe(store, url)
    old_seen = (START - timedelta(days=170)).isoformat()
    conn.execute(
        "INSERT INTO pruned_posts (feed_id, guid, seen_at) VALUES (?, 'old', ?)",
        (feed.id, old_seen),
    )
    fetcher = FakeFetcher({url: _ok(url, _rss("1"))})
    poller = _poller(store, fetcher, clock)
    await poller.poll_due()
    # A cached (unchanged) feed is not read again, so no tombstone is touched: only a full
    # read refreshes the ones it still offers ("old" is not in it).
    clock.now = _feed(store, feed.id).next_poll_at
    fetcher.responses[url] = _ok(url, b"unchanged, never parsed", from_cache=True)
    await poller.poll_due()
    seen = conn.execute("SELECT seen_at FROM pruned_posts WHERE feed_id = ?", (feed.id,))
    assert [row[0] for row in seen] == [old_seen]


def _tombstones(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT guid FROM pruned_posts ORDER BY guid")]


async def test_an_unchanged_feed_keeps_the_tombstones_of_items_it_still_lists(
    store: SqliteFeedStore, conn: sqlite3.Connection
) -> None:
    clock = Clock()
    url = "https://example.com/feed.xml"
    feed = _subscribe(store, url)
    fetcher = FakeFetcher({url: _ok(url, _rss("listed"))})
    poller = _poller(store, fetcher, clock)
    await poller.poll_due()
    posts = SqlitePostStore(conn)
    clock.now = START + timedelta(days=40)
    assert posts.prune(clock.now - timedelta(days=30), clock.now) == 1
    # The feed answers "unchanged" for a year: it still lists the item, so the tombstone
    # must outlive the TTL, or the item would come back as new at the next full read.
    fetcher.responses[url] = _ok(url, b"unchanged, never parsed", from_cache=True)
    for day in range(41, 400, 30):
        clock.now = START + timedelta(days=day)
        await poller.poll_all()
        posts.prune(clock.now - timedelta(days=30), clock.now)
    assert _tombstones(conn) == ["listed"]
    fetcher.responses[url] = _ok(url, _rss("listed"))
    await poller.poll_all()
    assert _guids(conn, feed.id) == []  # still pruned


async def test_items_dropped_from_a_feed_expire_once_it_is_reread_180_days_later(
    store: SqliteFeedStore, conn: sqlite3.Connection
) -> None:
    clock = Clock()
    url = "https://example.com/feed.xml"
    _subscribe(store, url)
    fetcher = FakeFetcher({url: _ok(url, _rss("dropped", "kept-listed"))})
    poller = _poller(store, fetcher, clock)
    await poller.poll_due()
    posts = SqlitePostStore(conn)
    clock.now = START + timedelta(days=40)
    assert posts.prune(clock.now - timedelta(days=30), clock.now) == 2
    fetcher.responses[url] = _ok(url, _rss("kept-listed"))  # "dropped" leaves the feed
    clock.now = START + timedelta(days=100)
    await poller.poll_all()
    posts.prune(clock.now - timedelta(days=30), clock.now)
    assert _tombstones(conn) == ["dropped", "kept-listed"]  # not yet 180 days
    clock.now = START + timedelta(days=40 + 181)
    await poller.poll_all()
    posts.prune(clock.now - timedelta(days=30), clock.now)
    assert _tombstones(conn) == ["kept-listed"]


class ThreadRecordingReader(FeedparserReader):
    def __init__(self) -> None:
        self.threads: list[int] = []

    def parse(self, body: bytes, url: str):  # type: ignore[no-untyped-def]
        self.threads.append(threading.get_ident())
        return super().parse(body, url)


async def test_parsing_runs_off_the_event_loop(store: SqliteFeedStore) -> None:
    url = "https://example.com/feed.xml"
    _subscribe(store, url)
    reader = ThreadRecordingReader()
    poller = Poller(store, FakeFetcher({url: _ok(url, _rss("1"))}), reader, Clock())
    await poller.poll_due()
    assert reader.threads and threading.get_ident() not in reader.threads

    added = ThreadRecordingReader()
    service = FeedService(
        store, FakeFetcher({"https://b.example/feed": _ok("https://b.example/feed", _rss("2"))}),
        added, Clock(),
    )  # fmt: skip
    await service.add("https://b.example/feed", None)
    assert added.threads and threading.get_ident() not in added.threads


async def test_poll_logs_name_the_host_never_the_path_or_credentials(
    store: SqliteFeedStore, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://feeds.example/private/token-abc123/feed.xml?key=s3cret"
    _subscribe(store, url)
    failing = FetchError(f"could not fetch {url}: HTTP 500 at {url}#frag")
    poller = _poller(store, FakeFetcher({url: failing}), Clock())
    with caplog.at_level(logging.DEBUG):
        await poller.poll_due()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "feeds.example" in logged
    for secret in ("token-abc123", "s3cret", "/private", "feed.xml"):
        assert secret not in logged, logged


async def test_a_stored_feed_error_keeps_only_the_hosts_of_urls(store: SqliteFeedStore) -> None:
    url = "https://feeds.example/feed.xml?key=s3cret"
    feed = _subscribe(store, url)
    target = "https://u:pw@cdn.example/x?t=tok"  # trufflehog:ignore
    failing = FetchError(f"could not fetch {url}: redirected to {target}")
    await _poller(store, FakeFetcher({url: failing}), Clock()).poll_due()
    error = _feed(store, feed.id).last_error
    assert error is not None and "feeds.example" in error and "cdn.example" in error
    for secret in ("s3cret", "pw", "tok", "feed.xml"):
        assert secret not in error, error


async def test_the_poller_ignores_deleted_feeds(
    store: SqliteFeedStore, conn: sqlite3.Connection
) -> None:
    url = "https://example.com/feed.xml"
    feed = _subscribe(store, url)
    assert store.delete_feed(feed.id, START)
    fetcher = FakeFetcher({url: _ok(url, _rss("1"))})
    summary = await _poller(store, fetcher, Clock()).poll_all()
    assert summary.polled == 0 and fetcher.calls == []
    # A poll already running when the feed was deleted adds nothing to it.
    assert (
        store.add_posts(feed.id, list(FeedparserReader().parse(_rss("2"), url).items), START) == 0
    )
    assert _guids(conn, feed.id) == []


class _Gate:
    """A fetcher whose answer for one URL waits until released (a slow feed in a round)."""

    def __init__(self, slow: str, body: bytes) -> None:
        self.slow, self.body = slow, body
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url == self.slow:
            await self.release.wait()
        return _ok(url, self.body)


async def test_a_feeds_retry_does_not_wait_behind_a_poll_round(store: SqliteFeedStore) -> None:
    slow = _subscribe(store, "https://slow.example/feed")
    quick = _subscribe(store, "https://quick.example/feed")
    gate = _Gate(slow.url, _rss("a"))
    poller = _poller(store, gate, Clock())
    round_ = asyncio.ensure_future(poller.poll_all())
    await asyncio.sleep(0.01)  # the round is now waiting on the slow feed
    await asyncio.wait_for(poller.poll_feed(quick.id), 1)  # Retry answers at once
    assert gate.calls.count(quick.url) >= 1
    gate.release.set()
    summary = await asyncio.wait_for(round_, 1)
    assert summary.failed == 0


async def test_a_round_and_a_retry_never_poll_one_feed_at_once(store: SqliteFeedStore) -> None:
    feed = _subscribe(store, "https://slow.example/feed")
    gate = _Gate(feed.url, _rss("a"))
    poller = _poller(store, gate, Clock())
    round_ = asyncio.ensure_future(poller.poll_all())
    await asyncio.sleep(0.01)
    retry = asyncio.ensure_future(poller.poll_feed(feed.id))
    await asyncio.sleep(0.01)
    assert gate.calls == [feed.url]  # the Retry waits for the round's poll of the same feed
    gate.release.set()
    await asyncio.wait_for(asyncio.gather(round_, retry), 1)
    assert gate.calls == [feed.url, feed.url]


async def test_a_retry_queued_behind_a_feed_that_is_then_unsubscribed_polls_nothing(
    store: SqliteFeedStore,
) -> None:
    feed = _subscribe(store, "https://slow.example/feed")
    gate = _Gate(feed.url, _rss("a"))
    poller = _poller(store, gate, Clock())
    round_ = asyncio.ensure_future(poller.poll_all())
    await asyncio.sleep(0.01)
    retry = asyncio.ensure_future(poller.poll_feed(feed.id))
    await asyncio.sleep(0.01)
    assert store.delete_feed(feed.id, START)
    gate.release.set()
    await asyncio.wait_for(asyncio.gather(round_, retry), 1)
    assert gate.calls == [feed.url]  # the queued Retry found it gone and fetched nothing
