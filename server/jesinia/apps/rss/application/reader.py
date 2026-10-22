# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Google Reader API use cases NetNewsWire calls (NetNewsWire sync spec §4).

Read state, Keep and feed management go through the same services as the reader, so a
change from NetNewsWire is a change in Jesinia. A star is a Keep; an unstar is recorded in
the star ledger only.
"""

from __future__ import annotations

import builtins
import hashlib
import html
import secrets
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime

from jesinia.apps.rss.application.feeds import FeedService, ImportResult
from jesinia.apps.rss.application.keep import KeepService
from jesinia.apps.rss.application.ports import ArticleRenderer, ReaderStore
from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.domain.feed import FeedExists, FeedView, FolderNotFound, validate_feed_url
from jesinia.apps.rss.domain.post import Cursor, PostNotFound
from jesinia.apps.rss.domain.reader import (
    FEED_PREFIX,
    READ,
    STARRED,
    FeedStream,
    InvalidStream,
    ItemQuery,
    ReaderAuthFailed,
    ReaderItem,
    StarUnavailable,
    WriteTokenRejected,
    label_name,
    parse_stream,
    write_token,
)


def _sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ReaderService:
    def __init__(
        self,
        store: ReaderStore,
        posts: PostService,
        feeds: FeedService,
        keeper: Callable[[], KeepService | None],
        renderer: ArticleRenderer,
        now: Callable[[], datetime],
    ) -> None:
        """``keeper`` gives the Keep service at each star (None while the KB is down)."""
        self._store = store
        self._posts = posts
        self._feeds = feeds
        self._keeper = keeper
        self._renderer = renderer
        self._now = now

    # -- credentials -------------------------------------------------------------------
    def has_password(self) -> bool:
        return self._store.reader_password() is not None

    def create_password(self) -> str:
        """A new reader password, returned once; the old one and every reader token issued
        under it stop working."""
        password = secrets.token_urlsafe(24)
        self._store.replace_reader_password(_sha256(password), self._now())
        return password

    def login(self, password: str) -> str:
        """A new reader token for the reader password. Raises ``ReaderAuthFailed``."""
        expected = self._store.reader_password()
        if expected is None or not secrets.compare_digest(_sha256(password), expected):
            raise ReaderAuthFailed("wrong reader password")
        token = secrets.token_urlsafe(32)
        self._store.add_reader_token(_sha256(token), self._now())
        return token

    def authenticate(self, token: str) -> None:
        """Raises ``ReaderAuthFailed`` unless ``token`` is a live reader token."""
        if not token or not self._store.has_reader_token(_sha256(token)):
            raise ReaderAuthFailed("unknown or revoked reader token")

    def check_write_token(self, token: str, t: str) -> None:
        """Raises ``WriteTokenRejected`` unless ``t`` is the write token for ``token``."""
        if not secrets.compare_digest(t.encode(), write_token(token).encode()):
            raise WriteTokenRejected("missing or stale write token")

    # -- reading -------------------------------------------------------------------------
    def subscriptions(self) -> builtins.list[tuple[FeedView, str | None]]:
        """Every subscribed feed with its site URL, in sidebar order."""
        return [(view, self._feeds.feed(view.id).site_url) for view in self._feeds.list()]

    def folders(self) -> builtins.list[str]:
        """Folder names in sidebar order (folders exist only while a feed is in them)."""
        return list(dict.fromkeys(v.folder for v in self._feeds.list() if v.folder is not None))

    def item_ids(self, query: ItemQuery) -> tuple[builtins.list[int], Cursor | None]:
        """A page of the stream's ids, and the continuation for the next page (if any)."""
        page = self._store.item_ids(
            ItemQuery(query.stream, query.exclude, query.oldest, query.limit + 1, query.after),
            self._now(),
        )
        kept = page[: query.limit]
        more = kept[-1][1] if len(page) > query.limit else None
        return [post_id for post_id, _ in kept], more

    def items(self, post_ids: Sequence[int]) -> builtins.list[ReaderItem]:
        return self._store.items(post_ids)

    def content(self, item: ReaderItem) -> str:
        """The item's HTML: the full article, else the feed's sanitized content, else the
        escaped snippet."""
        if item.full_markdown:
            return self._renderer.render(item.full_markdown, item.post.url)
        if item.post.content_html:
            return item.post.content_html
        return f"<p>{html.escape(item.post.snippet)}</p>"

    # -- item state -------------------------------------------------------------------
    async def edit_tags(
        self, post_ids: Sequence[int], add: Sequence[str], remove: Sequence[str]
    ) -> None:
        """Apply ``a=`` / ``r=`` tags. Read and starred mean something; any other tag (an item
        label) is ignored. Raises ``StarUnavailable``, or a Keep error, for a star."""
        if READ in add:
            self._posts.set_read(post_ids, True)
        if READ in remove:
            self._posts.set_read(post_ids, False)
        # A post pruned since the client last synced is skipped, as set_read skips it.
        if STARRED in add:
            for post_id in post_ids:
                with suppress(PostNotFound):
                    await self.star(post_id)
        if STARRED in remove:
            for post_id in post_ids:
                with suppress(PostNotFound):
                    self.unstar(post_id)

    async def star(self, post_id: int) -> None:
        """Keep the post, or, if it was kept before, only record the star again."""
        if self._store.latest_star(post_id) == "keep":
            return
        post = self._posts.get(post_id)
        if post.kept_page_id is not None:
            self._store.append_star(post_id, "keep", "netnewswire", post.kept_page_id, self._now())
            return
        keeper = self._keeper()
        if keeper is None:
            raise StarUnavailable("starring keeps the post, which needs the KB")
        await keeper.keep(post_id, "", [], None, source="netnewswire")

    def unstar(self, post_id: int) -> None:
        """Record the unstar; the KB page and the reader's Kept view are left as they are."""
        if self._store.latest_star(post_id) != "keep":
            return
        page_id = self._posts.get(post_id).kept_page_id
        self._store.append_star(post_id, "unkeep", "netnewswire", page_id, self._now())

    # -- subscriptions ---------------------------------------------------------------
    def _feed_id(self, stream_id: str) -> int:
        stream = parse_stream(stream_id)
        if not isinstance(stream, FeedStream):
            raise InvalidStream(f"{stream_id!r} is not a feed")
        return stream.feed_id

    def _folder_id(self, name: str) -> int:
        for view in self._feeds.list():
            if view.folder_id is not None and view.folder == name:
                return view.folder_id
        raise FolderNotFound(f"no folder {name!r}")

    async def quick_add(self, url: str) -> FeedView:
        """Subscribe (as the reader's Add does), or find the feed already subscribed."""
        try:
            feed = await self._feeds.add(url, None)
        except FeedExists as exc:
            # The URL given, or the feed a site advertises (found while adding).
            wanted = exc.url or validate_feed_url(url)
            for view in self._feeds.list():
                if view.url == wanted:
                    return view
            raise
        return self._feeds.view(feed.id)

    async def edit_subscription(
        self,
        stream_id: str,
        action: str,
        title: str | None,
        add: str | None,
        remove: str | None,
    ) -> None:
        """``subscription/edit``: subscribe, unsubscribe, rename, or move between folders."""
        if action == "subscribe":
            feed = await self.quick_add(stream_id.removeprefix(FEED_PREFIX))
            stream_id = f"{FEED_PREFIX}{feed.id}"
        feed_id = self._feed_id(stream_id)
        if action == "unsubscribe":
            self._feeds.remove(feed_id)
            return
        if title:
            self._feeds.rename(feed_id, title)
        target = label_name(add) if add else None
        if target is not None:
            self._feeds.move(feed_id, target)
        elif remove and label_name(remove) == self._feeds.view(feed_id).folder:
            self._feeds.move(feed_id, None)

    def rename_folder(self, tag: str, new_tag: str) -> None:
        old, new = label_name(tag), label_name(new_tag)
        if old is None or new is None:
            raise InvalidStream(f"{tag!r} or {new_tag!r} is not a folder")
        self._feeds.rename_folder(self._folder_id(old), new)

    def delete_folder(self, tag: str) -> None:
        name = label_name(tag)
        if name is None:
            raise InvalidStream(f"{tag!r} is not a folder")
        self._feeds.delete_folder(self._folder_id(name))

    async def import_opml(self, xml: str) -> ImportResult:
        return await self._feeds.import_opml(xml)
