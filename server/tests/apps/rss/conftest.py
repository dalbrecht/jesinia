# SPDX-License-Identifier: AGPL-3.0-or-later
"""A running server with kb, agents, fetch and rss, for the Keep and Engage tests."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from jesinia.apps.rss.application.ports import NewFeed
from jesinia.apps.rss.domain.feed import FIRST_INTERVAL_S, ParsedItem
from jesinia.apps.rss.infrastructure.store import SqliteFeedStore
from jesinia.apps.rss.module import RssModule
from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.idempotency import IDEMPOTENCY_HEADER
from jesinia.core.module import Module
from jesinia.services.agents.domain.events import TextDelta, TurnDone
from jesinia.services.agents.infrastructure.fake import FakeRunner
from jesinia.services.agents.module import AgentsModule
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.module import KbModule
from tests.apps.rss.test_posts import StubFetchModule
from tests.conftest import BASE_URL, bearer, running

KEEP_API = "/api/rss"
PUBLISHED = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
TAKEAWAY_TEXT = "WAL lets readers keep going while one writer appends."


@dataclass
class KeepEnv:
    app: FastAPI
    cfg: Config
    client: httpx.AsyncClient
    root: Path
    rss: RssModule
    conn: sqlite3.Connection
    runner: FakeRunner

    @property
    def kb(self) -> KbService:
        svc = self.app.state.ctx.services["kb"]
        assert isinstance(svc, KbService)
        return svc

    def post(
        self,
        url: str = "https://fly.io/blog/sqlite-wal",
        *,
        feed: str = "https://fly.io/blog/feed.xml",
        title: str = "What SQLite's WAL mode actually guarantees",
        discussion: str | None = None,
        content: str = "<p>The feed's own summary of the post.</p>",
    ) -> int:
        """A post in a feed (the feed is created on first use); its id."""
        feeds = SqliteFeedStore(self.conn)
        existing = feeds.feed_by_url(feed)
        feed_id = existing.id if existing is not None else feeds.insert_feed(
            NewFeed(feed, f"Feed {feed}", None, None, FIRST_INTERVAL_S, PUBLISHED, None, None)
        ).id  # fmt: skip
        guid = uuid.uuid4().hex
        item = ParsedItem(
            guid=guid,
            url=url,
            title=title,
            author="Ben Johnson",
            published_at=PUBLISHED,
            snippet="The feed's own summary",
            content_html=content,
            discussion_url=discussion,
            is_summary=True,
        )
        feeds.add_posts(feed_id, [item], PUBLISHED)
        row = self.conn.execute("SELECT id FROM posts WHERE guid = ?", (guid,)).fetchone()
        return int(row[0])

    def cache_article(self, post_id: int, markdown: str) -> None:
        self.conn.execute("UPDATE posts SET full_md = ? WHERE id = ?", (markdown, post_id))

    def state(self, post_id: int) -> tuple[str | None, str | None, str | None]:
        """The post's kept page id, archive state and archive error."""
        row = self.conn.execute(
            "SELECT kept_page_id, archive_state, archive_error FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        return row[0], row[1], row[2]

    def page_text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    async def keep(
        self,
        post_id: int,
        note: str = "",
        tags: list[str] | None = None,
        *,
        takeaway: dict[str, str] | None = None,
        key: str | None = None,
    ) -> httpx.Response:
        body: dict[str, object] = {"note": note, "tags": tags or []}
        if takeaway is not None:
            body["takeaway"] = takeaway
        headers = {IDEMPOTENCY_HEADER: key} if key is not None else {}
        return await self.client.post(
            f"{KEEP_API}/posts/{post_id}/keep", json=body, headers=headers
        )


async def _fresh_key(request: httpx.Request) -> None:
    request.headers.setdefault(IDEMPOTENCY_HEADER, str(uuid.uuid4()))


def fake_runner() -> FakeRunner:
    return FakeRunner([TextDelta(TAKEAWAY_TEXT), TurnDone("agent-1", "fake-model")])


@asynccontextmanager
async def keep_server(
    cfg: Config, root: Path | None, runner: FakeRunner | None = None
) -> AsyncIterator[KeepEnv]:
    """The server with ``root`` as its KB (None: no KB configured)."""
    c = replace(cfg, kb_path=root)
    runner = runner or fake_runner()
    rss = RssModule()
    modules: list[Module] = [
        KbModule(),
        AgentsModule(runners={"fake": runner}),
        StubFetchModule(),
        rss,
    ]
    app = create_app(c, modules, static_root=None)
    async with running(app):
        conn = sqlite3.connect(c.data_dir / "rss.sqlite", isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=bearer(c.token)
        ) as client:
            client.event_hooks["request"].append(_fresh_key)
            yield KeepEnv(app, c, client, root or Path("/nonexistent"), rss, conn, runner)
        conn.close()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


@pytest.fixture
async def keep_env(cfg: Config, kb_root: Path, mock: respx.MockRouter) -> AsyncIterator[KeepEnv]:
    async with keep_server(cfg, kb_root) as env:
        yield env
