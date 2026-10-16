# SPDX-License-Identifier: AGPL-3.0-or-later
"""OPML subscription lists: read with one folder level, write the current list."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable
from dataclasses import dataclass
from xml.sax.saxutils import escape

from jesinia.apps.rss.domain.feed import RssError

# Entity declarations are the XML bomb vector; OPML never needs a DTD, so refuse any.
_DOCTYPE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
# Always double-quoted; whitespace escaped so it survives attribute normalisation.
_ATTRIBUTE = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


class InvalidOpml(RssError):  # noqa: N818 - the error code clients see
    """The upload is not an OPML document."""


@dataclass(frozen=True)
class OpmlFeed:
    """One subscription; ``folder`` is the top-level folder it sits in, if any."""

    url: str
    title: str
    folder: str | None


def _label(outline: ElementTree.Element) -> str:
    return (outline.get("title") or outline.get("text") or "").strip()


def _collect(outline: ElementTree.Element, folder: str | None, out: list[OpmlFeed]) -> None:
    url = (outline.get("xmlUrl") or "").strip()
    if url:
        out.append(OpmlFeed(url, _label(outline), folder))
        return
    # A folder: its own name at the top level; deeper folders flatten into their top folder.
    inner = folder if folder is not None else (_label(outline) or None)
    for child in outline.findall("outline"):
        _collect(child, inner, out)


def parse_opml(xml: str) -> list[OpmlFeed]:
    """The feeds in an OPML document, in document order. Raises ``InvalidOpml``.

    Only one folder level is kept: a feed inside nested folders lands in its top folder.
    URLs are returned as written; the caller decides which it can subscribe to.
    """
    if _DOCTYPE.search(xml):
        raise InvalidOpml("document type declarations are not allowed in OPML")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise InvalidOpml(f"not well-formed XML: {exc}") from exc
    if root.tag != "opml":
        raise InvalidOpml(f"the root element is <{root.tag}>, not <opml>")
    body = root.find("body")
    if body is None:
        raise InvalidOpml("the document has no <body>")
    found: list[OpmlFeed] = []
    for outline in body.findall("outline"):
        _collect(outline, None, found)
    return found


def _quote(value: str) -> str:
    return f'"{escape(value, _ATTRIBUTE)}"'


def _feed_line(feed: OpmlFeed, indent: str) -> str:
    title = _quote(feed.title)
    return f'{indent}<outline type="rss" text={title} title={title} xmlUrl={_quote(feed.url)}/>'


def render_opml(feeds: Iterable[OpmlFeed]) -> str:
    """An OPML 2.0 document: unfiled feeds first, then each folder in first-seen order."""
    loose: list[OpmlFeed] = []
    folders: dict[str, list[OpmlFeed]] = {}
    for feed in feeds:
        if feed.folder is None:
            loose.append(feed)
        else:
            folders.setdefault(feed.folder, []).append(feed)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head><title>Jesinia subscriptions</title></head>",
        "  <body>",
        *(_feed_line(feed, "    ") for feed in loose),
    ]
    for name, members in folders.items():
        label = _quote(name)
        lines.append(f"    <outline text={label} title={label}>")
        lines.extend(_feed_line(feed, "      ") for feed in members)
        lines.append("    </outline>")
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)
