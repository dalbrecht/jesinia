# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep (spec §3.1, §3.3): write the post's KB page at once, then archive it.

Keep writes the page with the article text in hand (a loaded full article, else the feed's
content) and returns. The archive (the full article and up to 20 images, 10 MB) then runs
in the background and rewrites only the page's ``## Article`` section through a
revision-checked ``kb.write``. Its state (``archiving``, ``done``, ``failed`` with the cause)
lives on the post in ``rss.sqlite``, so the reader can show it and offer Retry.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from jesinia.apps.rss.application.ports import (
    ArticleConverter,
    FeedStore,
    ImageFetcher,
    PostStore,
)
from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.domain.kept import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_IMAGES,
    KeepResult,
    PostNotKept,
    Takeaway,
    asset_name,
    feed_article,
    find_images,
    is_http_url,
    kept_again,
    kept_front_matter,
    kept_title,
    link_to,
    render_kept_body,
    replace_article,
    rewrite_images,
    takeaway_block,
)
from jesinia.apps.rss.domain.post import PostView
from jesinia.services.fetch.application.service import FetchError
from jesinia.services.kb.application.errors import StaleWrite
from jesinia.services.kb.application.service import KbService, PageDraft, PageWithRev
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.domain.page import PageId

log = logging.getLogger(__name__)

# Characters that would turn a plain-text snippet into Markdown syntax.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>!])")


def _cause(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class KeepService:
    def __init__(
        self,
        kb: KbService,
        tags: TagService,
        posts: PostService,
        store: PostStore,
        feeds: FeedStore,
        converter: ArticleConverter,
        images: ImageFetcher,
        folder: Callable[[], str],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """``folder`` gives the ``folders.source`` setting at each Keep, so a change in
        Settings → RSS applies without a restart."""
        self.tags = tags
        self._kb = kb
        self._posts = posts
        self._store = store
        self._feeds = feeds
        self._converter = converter
        self._images = images
        self._folder = folder
        self._now = now
        # One Keep or archive write per source at a time, so they don't race each other.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._archives: dict[str, asyncio.Task[None]] = {}

    # -- keep ----------------------------------------------------------------------
    async def keep(
        self, post_id: int, note: str, tags: Sequence[str], takeaway: Takeaway | None
    ) -> KeepResult:
        """Write (or update) the post's page and start its archive when it needs one.

        A page for the same source is updated: the note is appended under a dated line,
        tags are merged and the article is left as it is. Raises ``PostNotFound``,
        ``KbUnavailable`` (checked first: nothing is written or queued), ``InvalidTag`` /
        ``NearDuplicate`` (nothing is written), or another ``KbError`` from the write.
        """
        post = self._posts.get(post_id)
        self._kb.require_available()
        names = await self.tags.ensure(tags) if tags else []
        at = self._now()
        ai_block = takeaway_block(takeaway, at) if takeaway is not None else None
        async with self._locks[post.url]:
            existing = self._kb.index.by_source(post.url)
            if existing is None:
                page = await self._create(post, note, names, ai_block, at)
            else:
                day = at.astimezone().date()
                page = await self._rewrite(
                    existing, lambda body: kept_again(body, note, day, names, ai_block)
                )
            page_id = page.page.id
            self._store.set_kept(post_id, page_id)
            state = self._store.page_archive_state(page_id)
            # A page archived before (or kept elsewhere) keeps its article: later edits to
            # it are the author's.
            if existing is None or state in ("archiving", "failed"):
                self._start_archive(page_id, post_id)
            else:
                self._store.set_archive_state(page_id, "done", None)
        return KeepResult(page_id, page.page.path, created=existing is None)

    async def _create(
        self, post: PostView, note: str, tags: list[str], ai_block: str | None, at: datetime
    ) -> PageWithRev:
        feed = self._feeds.feed(post.feed_id)
        article = self._posts.cached_article(post.id) or await self._feed_markdown(post)
        body = render_kept_body(note, tags, ai_block, article)
        front = kept_front_matter(post, feed.url if feed is not None else "", at.astimezone())
        draft = PageDraft(None, kept_title(post), PurePosixPath(self._folder()), body, front)
        return await self._kb.write(draft)

    async def _feed_markdown(self, post: PostView) -> str:
        """The feed's (already sanitised) content as Markdown, else its plain snippet."""
        converted = None
        if post.content_html:
            try:
                # A feed item's content is a fragment; the extractor wants a document.
                page = f"<html><body>{post.content_html}</body></html>"
                converted = await self._converter.convert(page.encode(), post.url)
            except FetchError as exc:  # Keep never fails because extraction did
                log.warning("converting the feed content of post %d failed: %s", post.id, exc)
        text = converted.markdown if converted else _MARKDOWN_SPECIAL.sub(r"\\\1", post.snippet)
        return feed_article(text)

    async def _write_changed(self, page_id: PageId, change: Callable[[str], str]) -> PageWithRev:
        current = await self._kb.read(page_id)
        page = current.page
        draft = PageDraft(page_id, page.title, page.path.parent, change(page.body), page.jesinia)
        return await self._kb.write(draft, expected_rev=current.rev)

    async def _rewrite(self, page_id: PageId, change: Callable[[str], str]) -> PageWithRev:
        """Apply ``change`` to the page's body against its current revision; if it is edited
        between the read and the write, once more against the fresh page."""
        try:
            return await self._write_changed(page_id, change)
        except StaleWrite:
            return await self._write_changed(page_id, change)

    # -- archive -------------------------------------------------------------------
    def _start_archive(self, page_id: str, post_id: int) -> None:
        self._store.set_archive_state(page_id, "archiving", None)
        self._schedule(page_id, post_id)

    def _schedule(self, page_id: str, post_id: int) -> None:
        running = self._archives.get(page_id)
        if running is not None and not running.done():
            return
        self._archives[page_id] = asyncio.create_task(
            self.archive(PageId(page_id), post_id), name=f"rss-archive:{page_id}"
        )

    def retry(self, post_id: int) -> PostView:
        """Archive the post's page again (``POST /posts/{id}/archive``). Raises
        ``PostNotFound`` or ``PostNotKept``."""
        post = self._posts.get(post_id)
        if post.kept_page_id is None:
            raise PostNotKept(f"post {post_id} is not kept")
        self._start_archive(post.kept_page_id, post_id)
        return self._posts.get(post_id)

    def recover(self) -> int:
        """Resume archives a stop interrupted (posts left ``archiving``); how many."""
        stranded = self._store.archiving()
        for page_id, post_id in stranded:
            self._schedule(page_id, post_id)
        return len(stranded)

    def pending(self) -> list[str]:
        """The pages whose archive is running."""
        return [page for page, task in self._archives.items() if not task.done()]

    async def drain(self) -> None:
        """Wait for every running archive (tests, and a clean stop)."""
        while running := [task for task in self._archives.values() if not task.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    async def stop(self) -> None:
        """Cancel running archives; they stay ``archiving`` and resume at the next start."""
        for task in self._archives.values():
            task.cancel()
        await asyncio.gather(*self._archives.values(), return_exceptions=True)
        self._archives.clear()

    async def archive(self, page_id: PageId, post_id: int) -> None:
        """Archive the full article and its images into the page, then record ``done``, or
        ``failed`` with the cause (the page keeps what Keep wrote)."""
        try:
            await self._archive(page_id, post_id)
        # Isolation boundary: whatever stops the archive (fetch, extraction, the KB) is
        # recorded on the post with its cause, for the reader's Retry.
        except Exception as exc:
            log.warning("archiving page %s (post %d) failed: %s", page_id, post_id, _cause(exc))
            self._store.set_archive_state(page_id, "failed", _cause(exc))
            return
        self._store.set_archive_state(page_id, "done", None)

    async def _archive(self, page_id: PageId, post_id: int) -> None:
        post = self._posts.get(post_id)
        markdown = await self._posts.full_article(post_id)
        folder = (await self._kb.read(page_id)).page.path.parent
        saved = await self._save_images(page_id, markdown)
        article = rewrite_images(markdown, {url: link_to(p, folder) for url, p in saved.items()})
        async with self._locks[post.url]:
            await self._rewrite(page_id, lambda body: replace_article(body, article))

    async def _save_images(self, page_id: PageId, markdown: str) -> dict[str, PurePosixPath]:
        """Download the article's images (http(s) only, through the guarded fetch) into the
        page's local-only asset folder, within the count and size caps. An image that is
        skipped stays linked to its original URL, and the reason is logged."""
        saved: dict[str, PurePosixPath] = {}
        used = 0
        for url in dict.fromkeys(ref.url for ref in find_images(markdown)):
            if not is_http_url(url):
                skipped = "not an http(s) URL"
            elif len(saved) >= ARCHIVE_MAX_IMAGES:
                skipped = f"the page already has {ARCHIVE_MAX_IMAGES} images"
            else:
                try:
                    result = await self._images.get(url, max_bytes=ARCHIVE_MAX_BYTES - used)
                    name = asset_name(result.body, result.content_type)
                    if result.status != 200 or name is None:
                        skipped = f"HTTP {result.status}, {result.content_type or 'no type'}"
                    else:
                        saved[url] = await self._kb.write_asset(
                            page_id, name, result.body, local_only=True
                        )
                        used += len(result.body)
                        continue
                # Isolation boundary: one image that cannot be fetched or stored (blocked,
                # too big, a transport error) is skipped; the archive goes on.
                except Exception as exc:
                    skipped = _cause(exc)
            log.info("archive of page %s skipped image %s: %s", page_id, url, skipped)
        return saved
