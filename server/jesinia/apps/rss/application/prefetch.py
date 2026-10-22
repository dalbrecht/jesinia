# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pull full articles ahead of time, so Reader API clients get the whole article (NetNewsWire
sync spec §5). Best-effort: a failure is recorded on the post and never retried here; the
reader's on-demand load still works, and polling and reading never wait on it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from jesinia.apps.rss.application.ports import PrefetchStore
from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.domain.feed import redact_urls
from jesinia.apps.rss.domain.post import ArticleUnavailable, PostNotFound

log = logging.getLogger(__name__)
# Posts tried per run (a job every minute): the fetch service paces each host anyway.
PREFETCH_BATCH = 20


class ArticlePrefetcher:
    def __init__(
        self, store: PrefetchStore, posts: PostService, now: Callable[[], datetime]
    ) -> None:
        self._store = store
        self._posts = posts
        self._now = now

    async def run(self, limit: int = PREFETCH_BATCH) -> int:
        """Try the full article of up to ``limit`` posts never tried, newest first; how many
        were tried."""
        tried = 0
        for post_id in self._store.posts_to_prefetch(limit):
            error: str | None = None
            try:
                await self._posts.full_article(post_id)
            except PostNotFound:
                continue  # pruned or unsubscribed meanwhile
            except ArticleUnavailable as exc:
                error = str(exc)
            # Isolation boundary: whatever stops one article (the extractor, the network) is
            # recorded on that post with its cause; the rest of the batch goes on.
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                log.warning(
                    "prefetching the article of post %d failed: %s", post_id, redact_urls(error)
                )
            self._store.record_article_attempt(post_id, self._now(), error)
            tried += 1
        return tried
