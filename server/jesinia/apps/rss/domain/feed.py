# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feeds, folders and parsed feed value objects, independent of any feed format."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

# A feed shows a warning after this many consecutive failed polls (spec §4.2).
WARN_AFTER_FAILURES = 3
# Every feed starts polling every 30 minutes, then adapts between these bounds (spec §4.2).
FIRST_INTERVAL_S = 30 * 60
MIN_INTERVAL_S = 15 * 60
MAX_INTERVAL_S = 12 * 60 * 60
# Past this many failures the backoff is at its cap; it also keeps 2**n small.
_BACKOFF_DOUBLINGS = 10
FOLDER_NAME_MAX = 100
CREDENTIALS_REFUSED = "credentials in URL are not supported"
# A URL candidate is the whole run of non-space characters after the scheme: parentheses
# and quotes can be part of a path or query (and so of a token), never a reason to stop.
_URL = re.compile(r"(?P<scheme>https?)://\S*", re.IGNORECASE)
# Sentence punctuation after a URL, handed back to the text rather than redacted with it.
_TRAILING = ".,;:!?'\"`"
_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}


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
    if has_credentials(url):
        # They would be stored, logged, exported and copied into kept pages (and git).
        raise InvalidFeedUrl(CREDENTIALS_REFUSED)
    return url


def strip_credentials(url: str) -> str:
    """``url`` without a user name or password, if it has one."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()


def has_credentials(url: str) -> bool:
    """Whether ``url`` carries a user name or password before its host."""
    return "@" in urlsplit(url).netloc


def host_of(url: str) -> str:
    """The host of ``url`` alone, for logs (never its path, query or credentials)."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def redact_urls(text: str) -> str:
    """``text`` with every URL cut down to its scheme and host, for logs: paths and query
    strings can carry tokens, and userinfo carries passwords."""
    return _URL.sub(_url_host, text)


def _url_host(match: re.Match[str]) -> str:
    candidate, tail = _split_trailing(match.group(0))
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        host = None
    if not host:
        return "<url>" + tail
    shown = f"[{host}]" if ":" in host else host
    return f"{match.group('scheme')}://{shown}{tail}"


def _split_trailing(candidate: str) -> tuple[str, str]:
    """The URL and the punctuation after it: trailing sentence marks, and closing brackets
    that close something opened before the URL (``(see https://h/x)``)."""
    end = len(candidate)
    while True:  # ends at the latest at the scheme, which is never punctuation
        last = candidate[end - 1]
        opener = _CLOSERS.get(last)
        unbalanced = opener is not None and (
            candidate[:end].count(last) > candidate[:end].count(opener)
        )
        if last not in _TRAILING and not unbalanced:
            break
        end -= 1
    return candidate[:end], candidate[end:]


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


def next_interval_s(interval_s: int, *, has_new_items: bool) -> int:
    """The polling interval after a successful poll: halved when it brought new items,
    grown by half when it brought none, kept within 15 minutes and 12 hours."""
    if has_new_items:
        return max(MIN_INTERVAL_S, interval_s // 2)
    return min(MAX_INTERVAL_S, int(interval_s * 1.5))


def failure_delay_s(fail_count: int) -> int:
    """How long to wait after the ``fail_count``-th failure in a row: 30 minutes doubled
    per failure, at most 12 hours."""
    return min(MAX_INTERVAL_S, FIRST_INTERVAL_S << min(fail_count, _BACKOFF_DOUBLINGS))


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
    # Unsubscribed: hidden and never polled, kept only for its kept posts.
    deleted: bool = False


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
