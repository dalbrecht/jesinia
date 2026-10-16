# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parsed feed value objects, independent of the feed format they came from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class FeedParseError(Exception):
    """The document is not a readable RSS, Atom or JSON Feed."""


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
