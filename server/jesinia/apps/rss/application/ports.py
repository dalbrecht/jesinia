# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports the RSS use cases depend on; infrastructure supplies the implementations."""

from __future__ import annotations

from typing import Protocol

from jesinia.apps.rss.domain.feed import ParsedFeed


class FeedReader(Protocol):
    def discover(self, html: bytes, base: str) -> str | None:
        """Return the absolute URL of the feed an HTML page advertises, if any."""
        ...

    def parse(self, body: bytes, url: str) -> ParsedFeed:
        """Parse an RSS, Atom or JSON Feed document; raise ``FeedParseError`` if unreadable."""
        ...
