# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feeds, folders and parsed feed value objects, independent of any feed format."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

# A feed shows a warning after this many consecutive failed polls (spec §4.2).
WARN_AFTER_FAILURES = 3
# Every feed starts polling every 30 minutes, then adapts (Task 10).
FIRST_INTERVAL_S = 30 * 60
FOLDER_NAME_MAX = 100


class RssError(Exception):
    """Base for the RSS app's errors; the class name is the ``error`` code clients see."""


class FeedParseError(RssError):
    """The document is not a readable RSS, Atom or JSON Feed."""


class InvalidFeedUrl(RssError):  # noqa: N818 - the error code clients see
    """A feed URL that is not an absolute http(s) URL."""


class InvalidFolderName(RssError):  # noqa: N818 - the error code clients see
    """A folder name that is blank or too long."""


class FeedNotFound(RssError):  # noqa: N818 - the error code clients see
    """No feed has this id."""


class FolderNotFound(RssError):  # noqa: N818 - the error code clients see
    """No folder has this id."""


class FeedExists(RssError):  # noqa: N818 - the error code clients see
    """The feed URL is already subscribed."""


class FolderExists(RssError):  # noqa: N818 - the error code clients see
    """Another folder already has this name."""


class FeedUnavailable(RssError):  # noqa: N818 - the error code clients see
    """The feed could not be fetched or read; the message carries the cause."""


def validate_feed_url(raw: str) -> str:
    """``raw`` trimmed, if it is an absolute http(s) URL with a host; else ``InvalidFeedUrl``."""
    url = raw.strip()
    try:
        parts = urlsplit(url)
        has_host = bool(parts.hostname)
    except ValueError as exc:
        raise InvalidFeedUrl(f"not a valid URL: {url}") from exc
    if parts.scheme not in ("http", "https"):
        raise InvalidFeedUrl("only http(s) feed URLs are supported")
    if not has_host:
        raise InvalidFeedUrl(f"the URL has no host: {url}")
    return url


def validate_folder_name(raw: str) -> str:
    """``raw`` trimmed, if it is 1 to ``FOLDER_NAME_MAX`` characters; else ``InvalidFolderName``."""
    name = raw.strip()
    if not 1 <= len(name) <= FOLDER_NAME_MAX:
        raise InvalidFolderName(f"a folder name must be 1 to {FOLDER_NAME_MAX} characters")
    return name


def feed_warning(fail_count: int, last_error: str | None) -> str | None:
    """What a feed shows once it has failed ``WARN_AFTER_FAILURES`` times in a row."""
    if fail_count < WARN_AFTER_FAILURES:
        return None
    return last_error or f"the feed failed {fail_count} times in a row"


@dataclass(frozen=True)
class Folder:
    id: int
    name: str
    position: int


@dataclass(frozen=True)
class Feed:
    """A subscription and its polling state."""

    id: int
    url: str
    title: str
    site_url: str | None
    folder_id: int | None
    interval_s: int
    next_poll_at: datetime
    fail_count: int
    last_error: str | None
    last_ok_at: datetime | None


@dataclass(frozen=True)
class FeedView:
    """A feed as the sidebar shows it: its folder, unread count and any warning."""

    id: int
    title: str
    url: str
    folder: str | None
    folder_id: int | None
    unread: int
    warning: str | None


@dataclass(frozen=True)
class ParsedItem:
    """One feed entry. ``snippet`` is plain text; ``content_html`` is already sanitised."""

    guid: str
    url: str
    title: str
    author: str | None
    published_at: datetime | None
    snippet: str
    content_html: str | None
    discussion_url: str | None
    is_summary: bool


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    site_url: str
    items: list[ParsedItem]
    # Set when the parser recovered from malformed input; carries the original cause.
    warning: str | None = None
