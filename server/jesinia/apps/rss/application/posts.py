# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading: the post lists, read state, full articles, headline search and retention."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from jesinia.apps.rss.application.ports import ArticleFetcher, PostStore
from jesinia.apps.rss.domain.post import (
    DEFAULT_RETENTION,
    PAGE_SIZE,
    SEARCH_LIMIT,
    ArticleUnavailable,
    Counts,
    Cursor,
    Page,
    PostNotFound,
    PostOutOfScope,
    PostView,
    View,
    check_page_size,
)
from jesinia.services.fetch.application.service import FetchError


class PostService:
    def __init__(
        self,
        store: PostStore,
        articles: ArticleFetcher,
        retention: Callable[[], timedelta] = lambda: DEFAULT_RETENTION,
    ) -> None:
        """``retention`` is read at each prune, so a changed ``rss.retention`` setting
        applies without a restart."""
        self._store = store
        self._articles = articles
        self._retention = retention

    def list(
        self,
        view: View,
        feed_id: int | None,
        folder_id: int | None,
        before: Cursor | None,
        limit: int = PAGE_SIZE,
    ) -> Page[PostView]:
        """A page of the view, newest first, optionally within one feed and/or folder.

        Raises ``InvalidPageSize``.
        """
        check_page_size(limit)
        found = self._store.posts(view, feed_id, folder_id, before, limit + 1)
        items = found[:limit]
        more = len(found) > limit
        return Page(items, items[-1].cursor if more else None)

    def get(self, post_id: int) -> PostView:
        post = self._store.post(post_id)
        if post is None:
            raise PostNotFound(f"no post {post_id}")
        return post

    def set_read(self, ids: Sequence[int], read: bool) -> int:
        """Set the read state of the given posts (unknown ids are skipped); how many."""
        return self._store.set_read(sorted(set(ids)), read) if ids else 0

    def mark_all_read(
        self, feed_id: int | None, folder_id: int | None, as_of: datetime
    ) -> builtins.list[int]:
        """Mark every unread post in the feed, folder or everywhere (both ``None``) read,
        except those first seen after ``as_of``; the ids marked, so the client can undo."""
        return self._store.mark_unread_read(feed_id, folder_id, as_of)

    def mark_read_above(
        self,
        post_id: int,
        view: View,
        feed_id: int | None,
        folder_id: int | None,
        as_of: datetime,
    ) -> int:
        """Mark the post and everything above it in the current list as read (``Shift+A``):
        only posts of that view, feed and folder, and only those first seen by ``as_of``
        (when the client loaded the list), so posts the reader never saw stay unread.
        Raises ``PostNotFound``, or ``PostOutOfScope`` (nothing marked) when the post is not
        in that list."""
        start = self.get(post_id).cursor
        # The unread view is checked by feed and folder only: selecting a post marks it read
        # while it stays on screen, so the selection is usually read by now.
        scope_view: View = "all" if view == "unread" else view
        if not self._store.in_scope(post_id, scope_view, feed_id, folder_id):
            raise PostOutOfScope(f"post {post_id} is not in the {view} list asked for")
        return self._store.mark_read_from(view, feed_id, folder_id, start, as_of)

    def cached_article(self, post_id: int) -> str | None:
        """The full article if it was loaded before (nothing is fetched)."""
        return self._store.full_md(self.get(post_id).id)

    async def full_article(self, post_id: int) -> str:
        """The original article as Markdown, fetched and extracted once, then cached.

        Raises ``PostNotFound``, or ``ArticleUnavailable`` with the cause.
        """
        post = self.get(post_id)
        cached = self._store.full_md(post_id)
        if cached is not None:
            return cached
        try:
            article = await self._articles.article(post.url)
        except FetchError as exc:
            raise ArticleUnavailable(f"could not fetch {post.url}: {exc}") from exc
        if article is None:
            raise ArticleUnavailable(f"no article text found at {post.url}")
        self._store.set_full_md(post_id, article.markdown)
        return article.markdown

    def search(self, q: str, limit: int = SEARCH_LIMIT) -> builtins.list[PostView]:
        """Retained posts whose headline or snippet contains ``q``, newest first. Case is
        ignored for ASCII letters only (SQLite ``LIKE``): "über" does not find "Über"."""
        text = q.strip()
        return self._store.search(text, limit) if text else []

    def prune(self, now: datetime) -> int:
        """Delete unkept posts older than the retention window; how many were deleted."""
        return self._store.prune(now - self._retention(), now)

    def counts(self) -> Counts:
        return self._store.counts()
