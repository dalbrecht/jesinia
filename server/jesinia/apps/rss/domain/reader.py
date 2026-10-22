# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Google Reader API's names for items, feeds, folders and states (NetNewsWire sync).

Item ids are the post's integer id: decimal in item-id lists, 16 hex digits in the long
``tag:google.com,2005:reader/item/…`` form (two's complement for negatives, as Google and
NetNewsWire do). A feed is ``feed/<id>``, a folder ``user/-/label/<name>``, and a state
``user/-/state/com.google/<state>``; any user id in place of ``-`` means the same user.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from jesinia.apps.rss.domain.feed import RssError
from jesinia.apps.rss.domain.post import Cursor, PostView

ITEM_PREFIX = "tag:google.com,2005:reader/item/"
STATE_PREFIX = "user/-/state/com.google/"
LABEL_PREFIX = "user/-/label/"
FEED_PREFIX = "feed/"
READING_LIST = f"{STATE_PREFIX}reading-list"
READ = f"{STATE_PREFIX}read"
STARRED = f"{STATE_PREFIX}starred"

State = Literal["reading-list", "read", "starred"]
# The star ledger: what happened to a post's star, and where it came from.
StarAction = Literal["keep", "unkeep"]
StarSource = Literal["reader", "netnewswire", "backfill"]
# A post first seen this recently whose full article has not been tried yet is held back
# from item lists: NetNewsWire downloads an item's content once (spec §4.2).
ARTICLE_HOLD_MINUTES = 15
ITEM_IDS_MAX = 10000
_STATES: dict[str, State] = {"reading-list": "reading-list", "read": "read", "starred": "starred"}
_USER_PATH = re.compile(r"^user/[^/]+/(state/com\.google|label)/(.+)$")
_HEX_ID = re.compile(r"^[0-9a-fA-F]{1,16}$")
_DECIMAL_ID = re.compile(r"^-?[0-9]{1,19}$")
_INT64 = 1 << 64


class InvalidItemId(RssError):  # noqa: N818 - the error code clients see
    """An item id that is neither a decimal nor a long-form (hex) Reader item id."""


class InvalidStream(RssError):  # noqa: N818 - the error code clients see
    """A stream id that names no feed, folder or known state."""


class ReaderAuthFailed(RssError):  # noqa: N818 - the error code clients see
    """A wrong reader password, or a reader token that is unknown or was revoked."""


class WriteTokenRejected(RssError):  # noqa: N818 - the error code clients see
    """A write without the ``T`` token issued for the caller's reader token."""


class StarUnavailable(RssError):  # noqa: N818 - the error code clients see
    """A star needs Keep, which needs the KB service; it is not running."""


@dataclass(frozen=True)
class StateStream:
    state: State


@dataclass(frozen=True)
class FeedStream:
    feed_id: int


@dataclass(frozen=True)
class LabelStream:
    name: str


Stream = StateStream | FeedStream | LabelStream


@dataclass(frozen=True)
class ReaderItem:
    """A post as the Reader API describes it."""

    post: PostView
    folder: str | None
    site_url: str | None
    is_starred: bool
    full_markdown: str | None


@dataclass(frozen=True)
class ItemQuery:
    """An item-id list request: a stream, minus a state, from a time, a page at a time."""

    stream: Stream
    exclude: State | None
    oldest: float | None  # Unix seconds
    limit: int
    after: Cursor | None  # continuation: posts below this place in newest-first order


def item_long_id(post_id: int) -> str:
    return f"{ITEM_PREFIX}{post_id % _INT64:016x}"


def parse_item_id(text: str) -> int:
    """A post id from the long (hex) form or the short (decimal) form. Raises
    ``InvalidItemId``."""
    value = text.strip()
    if value.startswith(ITEM_PREFIX):
        tail = value.removeprefix(ITEM_PREFIX)
        if not _HEX_ID.match(tail):
            raise InvalidItemId(f"not a Reader item id: {text!r}")
        number = int(tail, 16)
        return number - _INT64 if number >= _INT64 // 2 else number
    if not _DECIMAL_ID.match(value):
        raise InvalidItemId(f"not a Reader item id: {text!r}")
    return int(value)


def parse_stream(text: str) -> Stream:
    """Raises ``InvalidStream``."""
    if text.startswith(FEED_PREFIX):
        digits = text.removeprefix(FEED_PREFIX)
        if digits.isdecimal():
            return FeedStream(int(digits))
        raise InvalidStream(f"unknown feed stream {text!r}")
    found = _USER_PATH.match(text)
    if found is not None:
        kind, name = found.groups()
        if kind == "label":
            return LabelStream(name)
        state = _STATES.get(name)
        if state is not None:
            return StateStream(state)
    raise InvalidStream(f"unknown stream {text!r}")


def label_id(name: str) -> str:
    return f"{LABEL_PREFIX}{name}"


def label_name(tag: str) -> str | None:
    """The folder a ``…/label/<name>`` tag names, or None for any other tag."""
    found = _USER_PATH.match(tag)
    return found.group(2) if found is not None and found.group(1) == "label" else None


def feed_stream_id(feed_id: int) -> str:
    return f"{FEED_PREFIX}{feed_id}"


def write_token(reader_token: str) -> str:
    """The ``T`` write token for a reader token: derived, so it needs no storage, and 57
    characters, the length Google's tokens had (some clients check)."""
    digest = hashlib.sha256(b"jesinia-reader-write:" + reader_token.encode()).hexdigest()
    return digest[:57]
