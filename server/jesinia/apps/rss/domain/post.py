# SPDX-License-Identifier: AGPL-3.0-or-later
"""Posts as the reader lists them, the list views, paging cursors and retention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from jesinia.apps.rss.domain.feed import RssError

# The reader's lists (design pass): no "saved" view; Keep is the only way to hold a post.
View = Literal["unread", "kept", "all"]
# Unkept posts are pruned this long after publication, or after first sighting when undated
# (spec §4.4); ``rss.retention`` overrides it.
DEFAULT_RETENTION = timedelta(days=30)
# A pruned post's GUID is remembered until its feed has stopped offering it this long.
TOMBSTONE_TTL = timedelta(days=180)
PAGE_SIZE = 100
PAGE_SIZE_MAX = 500
SEARCH_LIMIT = 50

# Where a post sits in the newest-first list: its sort time (UTC ISO 8601) and id.
Cursor = tuple[str, int]


class PostNotFound(RssError):  # noqa: N818 - the error code clients see
    """No post has this id (it may have been pruned)."""


class ArticleUnavailable(RssError):  # noqa: N818 - the error code clients see
    """The full article could not be fetched or extracted; the message carries the cause."""


class InvalidCursor(RssError):  # noqa: N818 - the error code clients see
    """A ``before`` cursor that is not ``<ISO 8601 time with offset>,<post id>``."""


class InvalidPageSize(RssError):  # noqa: N818 - the error code clients see
    """A page size outside 1 to ``PAGE_SIZE_MAX``."""


def utc_text(moment: datetime) -> str:
    """``moment`` as stored and compared in ``rss.sqlite``: UTC ISO 8601, so text order is
    time order."""
    return moment.astimezone(UTC).isoformat()


def format_cursor(cursor: Cursor) -> str:
    return f"{cursor[0]},{cursor[1]}"


def parse_cursor(raw: str) -> Cursor:
    """The cursor ``format_cursor`` wrote; ``InvalidCursor`` for anything else."""
    moment_text, _, id_text = raw.rpartition(",")
    if not (id_text.isascii() and id_text.isdigit()):
        raise InvalidCursor(f"not a post cursor: {raw!r}")
    try:
        moment = datetime.fromisoformat(moment_text)
    except ValueError as exc:
        raise InvalidCursor(f"not a post cursor: {raw!r}") from exc
    if moment.tzinfo is None:
        raise InvalidCursor(f"the cursor time has no UTC offset: {raw!r}")
    return utc_text(moment), int(id_text)


def check_page_size(limit: int) -> int:
    if not 1 <= limit <= PAGE_SIZE_MAX:
        raise InvalidPageSize(f"a page holds 1 to {PAGE_SIZE_MAX} posts")
    return limit


@dataclass(frozen=True)
class PostView:
    """A post as the list and reader show it. ``content_html`` is already sanitised."""

    id: int
    feed_id: int
    feed_title: str
    url: str
    title: str
    author: str | None
    published_at: datetime | None
    first_seen_at: datetime
    snippet: str
    content_html: str | None
    discussion_url: str | None
    is_summary: bool
    read: bool
    kept_page_id: str | None
    archive_state: str | None
    archive_error: str | None
    has_full_article: bool

    @property
    def cursor(self) -> Cursor:
        """Its place in the newest-first list: dated by publication, else first sighting."""
        return utc_text(self.published_at or self.first_seen_at), self.id


@dataclass(frozen=True)
class Page[T]:
    """One page of a newest-first list; pass ``next_before`` back for the next page."""

    items: list[T]
    next_before: Cursor | None


@dataclass(frozen=True)
class Counts:
    """Unread posts per feed and per folder (every one, zero included), and the size of
    each view."""

    feeds: dict[int, int]
    folders: dict[int, int]
    unread: int
    kept: int
    all: int
