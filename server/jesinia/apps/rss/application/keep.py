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
import weakref
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from jesinia.apps.rss.application.ports import (
    ArticleConverter,
    FeedStore,
    ImageFetcher,
    PostStore,
    StarLedger,
)
from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.domain.feed import host_of, redact_urls
from jesinia.apps.rss.domain.kept import (
    ARCHIVE_MAX_ATTEMPTS,
    ARCHIVE_MAX_BYTES,
    ArchiveNotFailed,
    KeepResult,
    PostNotKept,
    Takeaway,
    archive_state,
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
from jesinia.apps.rss.domain.reader import StarSource
from jesinia.core.events import Event
from jesinia.services.fetch.application.service import FetchError, FetchTooLarge
from jesinia.services.kb.application.errors import StaleWrite
from jesinia.services.kb.application.service import KbService, PageDraft, PageWithRev
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.domain.page import Page, PageId

log = logging.getLogger(__name__)
# Archives running at once (each may fetch up to 20 images): a burst of Keeps or a restart
# with many left running must not hold them all in memory together.
ARCHIVE_CONCURRENCY = 3
# Published whenever a post's archive state changes, so the reader updates live (R22).
POST_CHANGED = "rss.post_changed"

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
        publish: Callable[[Event], Awaitable[None]] | None = None,
        ledger: StarLedger | None = None,
    ) -> None:
        """``folder`` gives the ``folders.source`` setting at each Keep, so a change in
        Settings → RSS applies without a restart. ``publish`` (the event bus, which never
        raises to the publisher) hears every archive state change as ``rss.post_changed``.
        ``ledger`` records each Keep as a star (NetNewsWire sync spec §6)."""
        self.tags = tags
        self._kb = kb
        self._posts = posts
        self._store = store
        self._feeds = feeds
        self._converter = converter
        self._images = images
        self._folder = folder
        self._now = now
        self._publish = publish
        self._ledger = ledger
        # One Keep or archive write per source at a time, so they don't race each other. A
        # lock lives only while someone holds or waits for it.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._archives: dict[str, asyncio.Task[None]] = {}
        self._archive_slots = asyncio.Semaphore(ARCHIVE_CONCURRENCY)

    def _lock(self, source: str) -> asyncio.Lock:
        lock = self._locks.get(source)
        if lock is None:
            lock = self._locks[source] = asyncio.Lock()
        return lock

    # -- keep ----------------------------------------------------------------------
    async def keep(
        self,
        post_id: int,
        note: str,
        tags: Sequence[str],
        takeaway: Takeaway | None,
        source: StarSource = "reader",
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
        async with self._lock(post.url):
            existing = self._kb.index.by_source(post.url)
            prior = self._store.page_archive_state(existing) if existing is not None else None
            # A page archived before (or kept elsewhere) keeps its article: later edits to
            # it are the author's.
            archive = existing is None or prior in ("archiving", "failed")
            if archive:
                # Recorded before the page is written, so a crash between the two leaves a
                # post that recovery finishes (``recover``), never a page nobody archives.
                self._store.set_post_archive_state(post_id, "archiving", None)
            try:
                if existing is None:
                    page = await self._create(post, note, names, ai_block, at)
                else:
                    day = at.astimezone().date()
                    page = await self._rewrite(
                        existing, lambda p: kept_again(p.body, note, day, names, ai_block)
                    )
            except BaseException:
                if archive:  # nothing was written: the post is as it was
                    self._store.set_post_archive_state(
                        post_id, archive_state(post.archive_state), post.archive_error
                    )
                raise
            page_id = page.page.id
            self._store.set_kept(post_id, page_id, "archiving" if archive else "done")
            self.record_star(post_id, page_id, source)
            await self._announce(page_id)
            if archive:
                self._start_archive(page_id, post_id)
        return KeepResult(page_id, page.page.path, created=existing is None)

    def record_star(self, post_id: int, page_id: str, source: StarSource) -> None:
        """Add a ``keep`` to the star ledger unless the post's latest entry already is one."""
        if self._ledger is not None and self._ledger.latest_star(post_id) != "keep":
            self._ledger.append_star(post_id, "keep", source, page_id, self._now())

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
                log.warning(
                    "converting the feed content of post %d failed: %s",
                    post.id,
                    redact_urls(str(exc)),
                )
        text = converted.markdown if converted else _MARKDOWN_SPECIAL.sub(r"\\\1", post.snippet)
        return feed_article(text)

    async def _write_changed(self, page_id: PageId, change: Callable[[Page], str]) -> PageWithRev:
        current = await self._kb.read(page_id)
        page = current.page
        draft = PageDraft(page_id, page.title, page.path.parent, change(page), page.jesinia)
        return await self._kb.write(draft, expected_rev=current.rev)

    async def _rewrite(self, page_id: PageId, change: Callable[[Page], str]) -> PageWithRev:
        """Write ``change(page)`` as the page's body against its current revision; if it is
        edited between the read and the write, once more against the fresh page."""
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
        task = asyncio.create_task(
            self._archive_in_turn(PageId(page_id), post_id), name=f"rss-archive:{page_id}"
        )
        self._archives[page_id] = task
        task.add_done_callback(lambda done: self._forget(page_id, done))

    def _forget(self, page_id: str, task: asyncio.Task[None]) -> None:
        if self._archives.get(page_id) is task:
            del self._archives[page_id]

    async def _announce(self, page_id: str) -> None:
        """Tell listeners (the PWAs, through ``/api/events``) the archive state of every post
        kept to the page."""
        if self._publish is None:
            return
        for post_id, state, error in self._store.archive_states_of_page(page_id):
            payload = {"id": post_id, "archive_state": state, "archive_error": error}
            await self._publish(Event(POST_CHANGED, payload))

    async def _archive_in_turn(self, page_id: PageId, post_id: int) -> None:
        # A retry or a resumed archive is "archiving" again: say so before it waits its turn.
        await self._announce(page_id)
        async with self._archive_slots:
            await self.archive(page_id, post_id)

    def retry(self, post_id: int) -> PostView:
        """Archive the post's page again after a failure (``POST /posts/{id}/archive``).
        Raises ``PostNotFound``, ``PostNotKept``, or ``ArchiveNotFailed`` unless the archive
        failed: a done one is never overwritten, a running one never started twice."""
        post = self._posts.get(post_id)
        if post.kept_page_id is None:
            raise PostNotKept(f"post {post_id} is not kept")
        state = self._store.page_archive_state(post.kept_page_id)
        if state != "failed":
            raise ArchiveNotFailed(f"the archive of post {post_id} is {state or 'not started'}")
        self._start_archive(post.kept_page_id, post_id)
        return self._posts.get(post_id)

    def recover(self) -> int:
        """Resume archives a stop interrupted (posts left ``archiving``); how many. A post
        marked before its page was written is matched to the page by source, or cleared when
        the page never got written."""
        resumed = 0
        for page_id, post_id in self._store.archiving():
            if page_id is None:
                found = self._kb.index.by_source(self._posts.get(post_id).url)
                if found is None:
                    self._store.set_post_archive_state(post_id, None, None)
                    continue
                self._store.set_kept(post_id, found, "archiving")
                page_id = found
            self._schedule(page_id, post_id)
            resumed += 1
        return resumed

    def pending(self) -> list[str]:
        """The pages whose archive is running."""
        return [page for page, task in self._archives.items() if not task.done()]

    async def drain(self) -> None:
        """Wait for every running archive (tests, and a clean stop)."""
        while running := [task for task in self._archives.values() if not task.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    async def stop(self) -> None:
        """Cancel running archives; they stay ``archiving`` and resume at the next start."""
        running = list(self._archives.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        self._archives.clear()

    async def archive(self, page_id: PageId, post_id: int) -> None:
        """Archive the full article and its images into the page, then record ``done``, or
        ``failed`` with the cause (the page keeps what Keep wrote)."""
        try:
            await self._archive(page_id, post_id)
        # Isolation boundary: whatever stops the archive (fetch, extraction, the KB) is
        # recorded on the post with its cause, for the reader's Retry.
        except Exception as exc:
            log.warning(
                "archiving page %s (post %d) failed: %s", page_id, post_id, redact_urls(_cause(exc))
            )
            self._store.set_archive_state(page_id, "failed", _cause(exc))
            await self._announce(page_id)
            return
        self._store.set_archive_state(page_id, "done", None)
        await self._announce(page_id)

    async def _archive(self, page_id: PageId, post_id: int) -> None:
        post = self._posts.get(post_id)
        markdown = await self._posts.full_article(post_id)
        saved = await self._save_images(page_id, markdown)

        def archived(page: Page) -> str:
            # Links are relative to where the page is now (it may have moved meanwhile).
            folder = page.path.parent
            local = {url: link_to(path, folder) for url, path in saved.items()}
            return replace_article(page.body, rewrite_images(markdown, local))

        async with self._lock(post.url):
            await self._rewrite(page_id, archived)

    async def _save_images(self, page_id: PageId, markdown: str) -> dict[str, PurePosixPath]:
        """Download the article's images (http(s) only, through the guarded fetch) into the
        page's local-only asset folder. Every download counts against the budget, kept or
        not: at most ``ARCHIVE_MAX_ATTEMPTS`` (which also bounds the images kept) and
        ``ARCHIVE_MAX_BYTES`` read. A body is kept only if its bytes are a raster image. An
        image that is skipped stays linked to its original URL, and the reason is logged."""
        saved: dict[str, PurePosixPath] = {}
        attempts = used = 0
        for url in dict.fromkeys(ref.url for ref in find_images(markdown)):
            if not is_http_url(url):
                skipped = "not an http(s) URL"
            elif attempts >= ARCHIVE_MAX_ATTEMPTS or used >= ARCHIVE_MAX_BYTES:
                skipped = "the archive's image budget is spent"
            else:
                attempts += 1
                try:
                    # Not cached: archived images live in the KB, not in fetch.sqlite too.
                    result = await self._images.get(
                        url, max_bytes=ARCHIVE_MAX_BYTES - used, cache=False
                    )
                    used += len(result.body)
                    name = asset_name(result.body) if result.status == 200 else None
                    if name is None:
                        skipped = f"HTTP {result.status}, not a PNG/JPEG/GIF/WebP/AVIF image"
                    else:
                        saved[url] = await self._kb.write_asset(
                            page_id, name, result.body, local_only=True
                        )
                        continue
                except FetchTooLarge as exc:  # it read what was left of the budget
                    used = ARCHIVE_MAX_BYTES
                    skipped = _cause(exc)
                # Isolation boundary: one image that cannot be fetched or stored (blocked, a
                # transport error) is skipped; the archive goes on.
                except Exception as exc:
                    skipped = _cause(exc)
            log.info(
                "archive of page %s skipped an image from %s: %s",
                page_id,
                host_of(url),
                redact_urls(skipped),
            )
        return saved
