# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import APIRouter, HTTPException

from jesinia.apps.rss.application import feeds as feeds_module
from jesinia.apps.rss.application.ports import NewFeed
from jesinia.apps.rss.domain.feed import (
    FeedNotFound,
    FeedParseError,
    FeedUnavailable,
    InvalidFeedUrl,
    InvalidFolderName,
    ParsedItem,
    feed_warning,
    has_credentials,
    host_of,
    redact_urls,
    validate_feed_url,
    validate_folder_name,
)
from jesinia.apps.rss.domain.opml import OpmlFeed, parse_opml, render_opml
from jesinia.apps.rss.infrastructure.store import SqliteFeedStore
from jesinia.apps.rss.module import RSS_DB, RssModule
from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.db import DbClass, open_db
from jesinia.core.idempotency import IDEMPOTENCY_HEADER, StoredResponse
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.fetch.application.ports import CachedFetch
from jesinia.services.fetch.application.service import FetchService
from tests.conftest import BASE_URL, bearer, running

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC = "93.184.216.34"
FEED_URL = "https://example.com/feed.xml"
API = "/api/rss"


class _NoCache:
    def get(self, url: str) -> CachedFetch | None:
        return None

    def put(self, url: str, entry: CachedFetch) -> None:
        return None

    def prune(self, older_than: timedelta) -> int:
        return 0


class StubFetchModule:
    """The real ``FetchService`` (SSRF checks included) with a fixed resolver and no pacing."""

    name = "fetch"

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        async def resolve(host: str) -> list[str]:
            return ["10.0.0.5"] if host == "internal.example" else [PUBLIC]

        ctx.services["fetch"] = FetchService(_NoCache(), resolve=resolve, min_interval=0)

    async def stop(self) -> None:
        return None

    def health(self) -> ModuleHealth:
        return ModuleHealth(True, "stub")


@dataclass
class Env:
    client: httpx.AsyncClient
    module: RssModule
    cfg: Config
    transport: httpx.ASGITransport

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.cfg.data_dir / "rss.sqlite", isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()


async def _fresh_key(request: httpx.Request) -> None:
    request.headers.setdefault(IDEMPOTENCY_HEADER, str(uuid.uuid4()))


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def env(cfg: Config, mock: respx.MockRouter) -> AsyncIterator[Env]:
    module = RssModule()
    app = create_app(cfg, [StubFetchModule(), module], static_root=None)
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=bearer(cfg.token)
        ) as client:
            client.event_hooks["request"].append(_fresh_key)
            yield Env(client, module, cfg, transport)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _serve(mock: respx.MockRouter, url: str, body: bytes, content_type: str) -> respx.Route:
    return mock.get(url).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": content_type})
    )


async def _add(env: Env, url: str = FEED_URL, folder: str | None = None) -> httpx.Response:
    return await env.client.post(f"{API}/feeds", json={"url": url, "folder": folder})


def _error(response: httpx.Response, code: int, error: str) -> str:
    assert response.status_code == code, response.text
    body = response.json()
    assert body["error"] == error
    detail: str = body["detail"]
    return detail


# --- domain rules ---------------------------------------------------------------------------


def test_feed_urls_must_be_absolute_http() -> None:
    assert validate_feed_url("  https://example.com/feed  ") == "https://example.com/feed"
    for bad in ("ftp://example.com/x", "javascript:alert(1)", "https://", "/feed", "http://[::1"):
        with pytest.raises(InvalidFeedUrl):
            validate_feed_url(bad)
    for secret in ("https://me:pw@example.com/feed", "https://token@example.com/feed"):
        with pytest.raises(InvalidFeedUrl, match="credentials in URL are not supported"):
            validate_feed_url(secret)


def test_log_helpers_keep_only_hosts() -> None:
    assert host_of("https://u:p@feeds.example:8443/a?b") == "feeds.example"
    assert host_of("not a url") == "?" and host_of("http://[::1") == "?"
    text = "fetch https://u:p@a.example/x?k=1 failed (see http://b.example) at HTTPS://C.example"
    assert redact_urls(text) == (
        "fetch https://a.example/… failed (see http://b.example/…) at HTTPS://C.example/…"
    )
    assert has_credentials("https://u@a.example/") and not has_credentials("https://a.example/@x")


def test_folder_names_are_trimmed_and_bounded() -> None:
    assert validate_folder_name("  Tech ") == "Tech"
    for bad in ("", "   ", "x" * 101):
        with pytest.raises(InvalidFolderName):
            validate_folder_name(bad)


def test_warning_shows_only_after_three_failures() -> None:
    assert feed_warning(2, "timeout") is None
    assert feed_warning(3, "timeout") == "timeout"
    assert feed_warning(3, None) == "the feed failed 3 times in a row"


# --- routes ---------------------------------------------------------------------------------


async def test_every_route_requires_the_owner(env: Env) -> None:
    anonymous = httpx.AsyncClient(transport=env.transport, base_url=BASE_URL)
    async with anonymous:
        calls = [
            anonymous.get(f"{API}/feeds"),
            anonymous.post(f"{API}/feeds", json={"url": FEED_URL}),
            anonymous.delete(f"{API}/feeds/1"),
            anonymous.patch(f"{API}/feeds/1", json={"folder": None}),
            anonymous.patch(f"{API}/folders/1", json={"name": "x"}),
            anonymous.post(f"{API}/opml", json={"opml": "<opml/>"}),
            anonymous.get(f"{API}/opml"),
            anonymous.post(f"{API}/refresh"),
        ]
        for call in calls:
            assert (await call).status_code == 401


async def test_add_feed_lists_it_with_its_folder_and_unread_count(
    env: Env, mock: respx.MockRouter
) -> None:
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    added = await _add(env, folder=" Tech ")
    assert added.status_code == 201, added.text
    view = added.json()
    assert view == {
        "id": view["id"],
        "title": "Example Blog",
        "url": FEED_URL,
        "folder": "Tech",
        "folder_id": view["folder_id"],
        "unread": 2,
        "warning": None,
    }
    assert (await env.client.get(f"{API}/feeds")).json() == [view]
    with env.db() as db:
        db.execute("UPDATE posts SET read = 1 WHERE guid = 'post-1'")
        row = db.execute("SELECT interval_s, fail_count, last_ok_at FROM feeds").fetchone()
    assert row[0] == 1800 and row[1] == 0 and row[2] is not None
    assert (await env.client.get(f"{API}/feeds")).json()[0]["unread"] == 1


async def test_add_feed_discovers_the_feed_a_site_advertises(
    env: Env, mock: respx.MockRouter
) -> None:
    _serve(mock, "https://site.example/", _fixture("html-with-link-alternate.html"), "text/html")
    _serve(mock, "https://site.example/feeds/atom.xml", _fixture("atom.xml"), "application/xml")
    added = await _add(env, "https://site.example/")
    assert added.status_code == 201, added.text
    assert added.json()["url"] == "https://site.example/feeds/atom.xml"
    assert added.json()["folder"] is None
    again = await _add(env, "https://site.example/")
    assert "already subscribed" in _error(again, 409, "FeedExists")


async def test_add_falls_back_to_discovery_when_a_non_html_body_is_not_a_feed(
    env: Env, mock: respx.MockRouter
) -> None:
    page = _fixture("html-with-link-alternate.html")
    _serve(mock, "https://odd.example/", page, "application/octet-stream")
    _serve(mock, "https://odd.example/feeds/atom.xml", _fixture("atom.xml"), "application/xml")
    added = await _add(env, "https://odd.example/")
    assert added.status_code == 201, added.text


async def test_add_parses_a_feed_served_as_html(env: Env, mock: respx.MockRouter) -> None:
    # A common misconfiguration: the feed itself is labelled text/html and links no feed.
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "text/html; charset=utf-8")
    added = await _add(env)
    assert added.status_code == 201, added.text
    assert added.json()["url"] == FEED_URL and added.json()["unread"] == 2


async def test_add_existing_feed_is_a_conflict(env: Env, mock: respx.MockRouter) -> None:
    route = _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    assert (await _add(env)).status_code == 201
    _error(await _add(env), 409, "FeedExists")
    assert route.call_count == 1  # an existing URL is refused before any fetch


async def test_add_rejects_urls_that_are_not_http(env: Env) -> None:
    for url in ("ftp://example.com/feed", "javascript:alert(1)", "feed.xml"):
        _error(await _add(env, url), 400, "InvalidFeedUrl")
    _error(await _add(env, folder="   "), 400, "InvalidFolderName")


async def test_add_reports_why_a_feed_could_not_be_added(env: Env, mock: respx.MockRouter) -> None:
    mock.get("https://gone.example/feed").mock(return_value=httpx.Response(404))
    assert "HTTP 404" in _error(
        await _add(env, "https://gone.example/feed"), 502, "FeedUnavailable"
    )

    detail = _error(await _add(env, "http://internal.example/feed"), 502, "FeedUnavailable")
    assert "public address" in detail

    _serve(mock, "https://plain.example/", b"<html><head></head></html>", "text/html")
    detail = _error(await _add(env, "https://plain.example/"), 502, "FeedUnavailable")
    assert "no feed" in detail
    with pytest.raises(FeedUnavailable) as info:
        await env.module.service.add("https://plain.example/", None)
    assert isinstance(info.value.__cause__, FeedParseError)  # the parse attempt is kept

    _serve(mock, "https://junk.example/x", _fixture("broken.xml"), "application/xml")
    detail = _error(await _add(env, "https://junk.example/x"), 502, "FeedUnavailable")
    assert "not a readable feed" in detail

    page = b'<html><head><link rel="alternate" type="application/rss+xml" href="/f"></head></html>'
    _serve(mock, "https://loop.example/", page, "text/html")
    _serve(mock, "https://loop.example/f", b"<html>still not a feed</html>", "text/html")
    detail = _error(await _add(env, "https://loop.example/"), 502, "FeedUnavailable")
    assert "https://loop.example/f" in detail
    assert (await env.client.get(f"{API}/feeds")).json() == []


async def test_a_recovered_parse_keeps_its_warning_as_the_last_error(
    env: Env, mock: respx.MockRouter
) -> None:
    whole = _fixture("rss2.xml")
    body = whole[: whole.index(b"<item>", whole.index(b"</item>")) + 30]  # truncated mid-item
    _serve(mock, FEED_URL, body, "application/rss+xml")
    assert (await _add(env)).status_code == 201
    with env.db() as db:
        last_error = db.execute("SELECT last_error FROM feeds").fetchone()[0]
        assert last_error
        db.execute("UPDATE feeds SET fail_count = 3")
    assert (await env.client.get(f"{API}/feeds")).json()[0]["warning"] == last_error


async def test_delete_feed(env: Env, mock: respx.MockRouter) -> None:
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    feed_id = (await _add(env, folder="Tech")).json()["id"]
    removed = await env.client.delete(f"{API}/feeds/{feed_id}")
    assert removed.status_code == 204 and removed.content == b""
    assert (await env.client.get(f"{API}/feeds")).json() == []
    with env.db() as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM folders").fetchone()[0] == 0
    _error(await env.client.delete(f"{API}/feeds/{feed_id}"), 404, "FeedNotFound")
    with pytest.raises(FeedNotFound):
        env.module.service.view(feed_id)


async def test_patch_feed_moves_it_between_folders(env: Env, mock: respx.MockRouter) -> None:
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    feed_id = (await _add(env, folder="Tech")).json()["id"]
    moved = await env.client.patch(f"{API}/feeds/{feed_id}", json={"folder": "News"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder"] == "News"
    loose = await env.client.patch(f"{API}/feeds/{feed_id}", json={"folder": None})
    assert loose.json()["folder"] is None and loose.json()["folder_id"] is None
    with env.db() as db:
        assert db.execute("SELECT COUNT(*) FROM folders").fetchone()[0] == 0
    missing = await env.client.patch(f"{API}/feeds/999", json={"folder": "News"})
    _error(missing, 404, "FeedNotFound")


async def test_patch_folder_renames_it(env: Env, mock: respx.MockRouter) -> None:
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    _serve(mock, "https://example.com/atom", _fixture("atom.xml"), "application/atom+xml")
    tech = (await _add(env, folder="Tech")).json()["folder_id"]
    assert (await _add(env, "https://example.com/atom", folder="News")).status_code == 201
    renamed = await env.client.patch(f"{API}/folders/{tech}", json={"name": " Reading "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {"id": tech, "name": "Reading"}
    folders = {f["folder"] for f in (await env.client.get(f"{API}/feeds")).json()}
    assert folders == {"Reading", "News"}
    same = await env.client.patch(f"{API}/folders/{tech}", json={"name": "Reading"})
    assert same.status_code == 200
    taken = await env.client.patch(f"{API}/folders/{tech}", json={"name": "News"})
    _error(taken, 409, "FolderExists")
    _error(await env.client.patch(f"{API}/folders/999", json={"name": "x"}), 404, "FolderNotFound")
    blank = await env.client.patch(f"{API}/folders/{tech}", json={"name": "  "})
    _error(blank, 400, "InvalidFolderName")


async def test_import_opml_skips_existing_and_reports_failures(
    env: Env, mock: respx.MockRouter
) -> None:
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    assert (await _add(env, folder="Tech")).status_code == 201
    opml = render_opml(
        [
            OpmlFeed(FEED_URL, "Already here", "Elsewhere"),
            OpmlFeed("https://new.example/rss", "New one", "Tech"),
            OpmlFeed("https://loose.example/atom", "", None),
            OpmlFeed("https://new.example/rss", "Listed twice", "Tech"),
            OpmlFeed("javascript:alert(1)", "Hostile", "Tech"),
        ]
    )
    imported = await env.client.post(f"{API}/opml", json={"opml": opml})
    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert result["added"] == 2 and result["existing"] == 2
    assert result["failed"] == [
        {"url": "javascript:alert(1)", "cause": "only http(s) feed URLs are supported"}
    ]
    views = {v["url"]: v for v in (await env.client.get(f"{API}/feeds")).json()}
    assert views[FEED_URL]["folder"] == "Tech"  # an existing feed is left where it is
    assert views["https://new.example/rss"]["title"] == "New one"
    assert views["https://new.example/rss"]["folder"] == "Tech"
    assert views["https://loose.example/atom"]["title"] == "https://loose.example/atom"
    with env.db() as db:
        due = db.execute(
            "SELECT next_poll_at FROM feeds WHERE url = 'https://new.example/rss'"
        ).fetchone()[0]
    assert datetime.fromisoformat(due) <= datetime.now(UTC)  # polled on the next tick

    bad = await env.client.post(f"{API}/opml", json={"opml": "<rss/>"})
    assert "<rss>" in _error(bad, 400, "InvalidOpml")


async def test_export_opml_writes_every_feed(env: Env, mock: respx.MockRouter) -> None:
    _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    assert (await _add(env, folder="Tech")).status_code == 201
    exported = await env.client.get(f"{API}/opml")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/x-opml")
    assert "attachment" in exported.headers["content-disposition"]
    assert parse_opml(exported.text) == [OpmlFeed(FEED_URL, "Example Blog", "Tech")]


async def test_writes_need_an_idempotency_key_and_replay_it(
    env: Env, mock: respx.MockRouter
) -> None:
    route = _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    missing = await env.client.post(
        f"{API}/feeds", json={"url": FEED_URL}, headers={IDEMPOTENCY_HEADER: ""}
    )
    assert missing.status_code == 400
    headers = {IDEMPOTENCY_HEADER: "add-once"}
    first = await env.client.post(f"{API}/feeds", json={"url": FEED_URL}, headers=headers)
    second = await env.client.post(f"{API}/feeds", json={"url": FEED_URL}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and route.call_count == 1


async def test_module_clears_pending_keys_and_prunes_old_ones(cfg: Config) -> None:
    conn = open_db(cfg.data_dir, RSS_DB)
    conn.execute(
        "INSERT INTO idempotency (key, route, status, body, created_at) "
        "VALUES ('k', 'POST /api/rss/feeds', NULL, NULL, '2026-01-01T00:00:00+00:00')"
    )
    conn.close()
    module = RssModule()
    app = create_app(cfg, [StubFetchModule(), module], static_root=None)
    async with running(app):
        store = module.idempotency
        assert store.reserve("k", "POST /api/rss/feeds", datetime.now(UTC)) is None
        old = datetime.now(UTC) - timedelta(days=30)
        assert store.reserve("old", "POST /api/rss/feeds", old) is None
        store.complete("old", "POST /api/rss/feeds", StoredResponse(204, None))
        await app.state.ctx.scheduler.run_now("rss.idempotency-prune")
        assert store.reserve("old", "POST /api/rss/feeds", datetime.now(UTC)) is None
        assert module.health().ok
    assert not module.health().ok
    await module.prune_idempotency_keys()  # stopped: nothing to prune, no error


async def test_module_needs_the_fetch_service(cfg: Config) -> None:
    module = RssModule()
    app = create_app(cfg, [module], static_root=None)
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=bearer(cfg.token)
        ) as client:
            assert (await client.get(f"{API}/feeds")).status_code == 503
            health = (await client.get("/healthz")).json()["modules"]["rss"]
            assert health["ok"] is False and "fetch" in health["detail"]
    for unavailable in (lambda: module.service, lambda: module.idempotency):
        with pytest.raises(Exception, match="rss module not started"):
            unavailable()


def test_rss_db_is_history() -> None:
    assert RSS_DB.name == "rss" and RSS_DB.db_class is DbClass.HISTORY


def test_store_adds_posts_all_or_nothing(cfg: Config) -> None:
    conn = open_db(cfg.data_dir, RSS_DB)
    store = SqliteFeedStore(conn)
    now = datetime.now(UTC)
    feed = store.insert_feed(NewFeed(FEED_URL, "F", None, None, 1800, now, None, None))
    item = ParsedItem("g1", "https://e.example/1", "One", None, now, "", None, None, True)
    with pytest.raises(sqlite3.IntegrityError):  # no feed 999: the batch rolls back
        store.add_posts(999, [item, replace(item, guid="g2")], now)
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert not conn.in_transaction
    assert store.add_posts(feed.id, [item, item], now) == 1
    assert store.add_posts(feed.id, [item], now) == 0
    conn.close()


async def test_refresh_route_polls_every_feed_now(env: Env, mock: respx.MockRouter) -> None:
    route = _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    assert (await _add(env)).status_code == 201
    missing = await env.client.post(f"{API}/refresh", headers={IDEMPOTENCY_HEADER: ""})
    assert missing.status_code == 400
    refreshed = await env.client.post(f"{API}/refresh", headers={IDEMPOTENCY_HEADER: "r1"})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == {"polled": 1, "new_items": 0, "failed": 0}
    assert route.call_count == 2  # the add, then the refresh (the feed was not due)
    with env.db() as db:
        interval = db.execute("SELECT interval_s FROM feeds").fetchone()[0]
    assert interval == 2700  # nothing new: the next poll backs off


async def test_module_polls_due_feeds_on_its_schedule(env: Env, mock: respx.MockRouter) -> None:
    route = _serve(mock, FEED_URL, _fixture("rss2.xml"), "application/rss+xml")
    opml = render_opml([OpmlFeed(FEED_URL, "Imported", None)])
    assert (await env.client.post(f"{API}/opml", json={"opml": opml})).status_code == 200
    await env.module.poll_due()
    assert route.call_count == 1
    assert (await env.client.get(f"{API}/feeds")).json()[0]["unread"] == 2
    stopped = RssModule()
    await stopped.poll_due()  # a tick before start (or after stop) does nothing
    with pytest.raises(HTTPException):
        _ = stopped.poller


async def test_feed_urls_with_credentials_are_refused(env: Env, mock: respx.MockRouter) -> None:
    route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    refused = await _add(env, "https://me:s3cret@private.example/feed")
    detail = _error(refused, 400, "InvalidFeedUrl")
    assert detail == "credentials in URL are not supported" and "s3cret" not in refused.text
    opml = render_opml([OpmlFeed("https://me:s3cret@private.example/feed", "Paid", None)])
    imported = (await env.client.post(f"{API}/opml", json={"opml": opml})).json()
    assert imported["added"] == 0
    assert [f["cause"] for f in imported["failed"]] == ["credentials in URL are not supported"]
    assert (await env.client.get(f"{API}/feeds")).json() == [] and route.call_count == 0


async def test_a_site_advertising_a_feed_with_credentials_is_refused(
    env: Env, mock: respx.MockRouter
) -> None:
    page = b'<html><head><link rel="alternate" type="application/rss+xml" href="https://u:p@example.com/feed.xml"></head></html>'
    _serve(mock, "https://example.com/", page, "text/html")
    refused = await _add(env, "https://example.com/")
    assert _error(refused, 400, "InvalidFeedUrl") == "credentials in URL are not supported"


async def test_opml_import_is_all_or_nothing_and_parses_off_the_loop(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[int] = []
    real_parse = parse_opml

    def recording(xml: str) -> list[OpmlFeed]:
        threads.append(threading.get_ident())
        return real_parse(xml)

    monkeypatch.setattr(feeds_module, "parse_opml", recording)
    opml = render_opml([OpmlFeed(f"https://f{n}.example/rss", f"F{n}", "Tech") for n in range(3)])
    store = env.module.service._store  # the module's own store
    real_insert = store.insert_feed
    inserted: list[str] = []

    def failing(feed: NewFeed):  # type: ignore[no-untyped-def]
        if len(inserted) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        inserted.append(feed.url)
        return real_insert(feed)

    monkeypatch.setattr(store, "insert_feed", failing)
    with pytest.raises(sqlite3.OperationalError):
        await env.client.post(f"{API}/opml", json={"opml": opml})
    assert threads and threading.get_ident() not in threads
    monkeypatch.undo()
    assert (await env.client.get(f"{API}/feeds")).json() == []  # the first two rolled back
    assert (await env.client.post(f"{API}/opml", json={"opml": opml})).json()["added"] == 3


async def test_a_fresh_start_prunes_expired_posts_and_keys(cfg: Config) -> None:
    conn = open_db(cfg.data_dir, RSS_DB)
    store = SqliteFeedStore(conn)
    long_ago = datetime.now(UTC) - timedelta(days=60)
    feed = store.insert_feed(NewFeed(FEED_URL, "F", None, None, 1800, long_ago, None, None))
    old = ParsedItem("old", "https://e.example/1", "Old", None, long_ago, "", None, None, True)
    store.add_posts(feed.id, [old], long_ago)
    conn.execute(
        "INSERT INTO idempotency (key, route, status, body, created_at) "
        "VALUES ('k', 'POST /api/rss/feeds', 204, NULL, ?)",
        (long_ago.isoformat(),),
    )
    module = RssModule()
    app = create_app(cfg, [StubFetchModule(), module], static_root=None)
    async with running(app):
        for _ in range(200):
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM posts), (SELECT COUNT(*) FROM idempotency)"
            ).fetchone()
            if counts == (0, 0):
                break
            await asyncio.sleep(0.01)
    # Without waiting a day of uptime: the daily jobs also run once at start.
    assert counts == (0, 0)
    conn.close()
