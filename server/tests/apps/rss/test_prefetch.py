# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full articles pulled ahead of time (NetNewsWire sync spec §5)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import respx

from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.application.prefetch import ArticlePrefetcher
from jesinia.apps.rss.domain.post import PostNotFound
from jesinia.apps.rss.infrastructure.render import MarkdownArticleRenderer
from jesinia.apps.rss.module import RSS_DB
from jesinia.core.db import open_db
from tests.apps.rss.conftest import KeepEnv

ARTICLE = Path(__file__).parents[2] / "services" / "fetch" / "fixtures" / "article.html"


def _attempt(env: KeepEnv, post_id: int) -> tuple[str | None, str | None, bool]:
    row = env.conn.execute(
        "SELECT article_attempted_at, article_error, full_md IS NOT NULL FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    return row[0], row[1], bool(row[2])


async def test_prefetch_should_pull_new_articles_and_record_failures(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    ok = keep_env.post(url="https://example.com/ok")
    broken = keep_env.post(url="https://example.com/broken")
    cached = keep_env.post(url="https://example.com/cached")
    keep_env.cache_article(cached, "already here")
    mock.get("https://example.com/ok").mock(
        return_value=httpx.Response(
            200, content=ARTICLE.read_bytes(), headers={"content-type": "text/html"}
        )
    )
    mock.get("https://example.com/broken").mock(return_value=httpx.Response(503))

    await keep_env.rss.prefetch_articles()
    attempted, error, has_full = _attempt(keep_env, ok)
    assert attempted is not None and error is None and has_full
    attempted, error, has_full = _attempt(keep_env, broken)
    assert attempted is not None and "503" in (error or "") and not has_full
    assert _attempt(keep_env, cached) == (None, None, True)  # loaded already: left alone

    calls = mock.calls.call_count
    await keep_env.rss.prefetch_articles()  # nothing left: a failure is not retried
    assert mock.calls.call_count == calls


class _Store:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.recorded: list[tuple[int, str | None]] = []

    def posts_to_prefetch(self, limit: int) -> list[int]:
        return self.ids[:limit]

    def record_article_attempt(self, post_id: int, at: datetime, error: str | None) -> None:
        self.recorded.append((post_id, error))


class _Posts:
    async def full_article(self, post_id: int) -> str:
        if post_id == 1:
            raise PostNotFound("pruned")
        raise RuntimeError("extractor crashed")


async def test_prefetch_should_isolate_unexpected_failures_and_skip_pruned_posts() -> None:
    store = _Store([1, 2])
    prefetcher = ArticlePrefetcher(store, cast(PostService, _Posts()), lambda: datetime.now(UTC))
    assert await prefetcher.run() == 1
    assert store.recorded == [(2, "extractor crashed")]


async def test_prefetch_job_should_do_nothing_before_start() -> None:
    from jesinia.apps.rss.module import RssModule

    await RssModule().prefetch_articles()


def test_renderer_should_escape_raw_html_and_resolve_links() -> None:
    html = MarkdownArticleRenderer().render(
        "<b>raw</b>\n\n![pic](img.png)\n\n| a |\n|---|\n| 1 |", "https://ex.example/post/"
    )
    assert "&lt;b&gt;raw&lt;/b&gt;" in html
    assert 'src="https://ex.example/post/img.png"' in html
    assert "<table>" in html


def test_migration_should_backfill_the_ledger_and_mark_cached_articles(tmp_path: Path) -> None:
    conn = open_db(tmp_path, RSS_DB)
    conn.close()
    raw = sqlite3.connect(tmp_path / "rss.sqlite", isolation_level=None)
    sql = Path(__file__).parents[3] / "jesinia/apps/rss/migrations/0006_reader_sync.sql"
    raw.execute("DELETE FROM star_ledger")
    raw.execute(
        "INSERT INTO feeds (id, url, title, interval_s, next_poll_at) "
        "VALUES (1, 'https://f.example/x', 'F', 900, '2026-10-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO posts (id, feed_id, guid, url, title, first_seen_at, snippet, kept_page_id, "
        "full_md, is_summary) VALUES (7, 1, 'g', 'https://f.example/p', 'P', "
        "'2026-10-01T00:00:00+00:00', 's', 'page-1', 'md', 0)"
    )
    raw.execute("UPDATE posts SET article_attempted_at = NULL")
    # Re-run only the data statements of the migration against the seeded row.
    statements = [s for s in sql.read_text().split(";") if "INSERT INTO star_ledger" in s
                  or "UPDATE posts SET article_attempted_at" in s]  # fmt: skip
    for statement in statements:
        raw.execute(statement)
    assert raw.execute("SELECT post_id, action, source, page_id FROM star_ledger").fetchall() == [
        (7, "keep", "backfill", "page-1")
    ]
    assert raw.execute("SELECT article_attempted_at FROM posts").fetchone()[0] == (
        "2026-10-01T00:00:00+00:00"
    )
    raw.close()
