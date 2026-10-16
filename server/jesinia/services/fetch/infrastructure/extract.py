# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ArticleExtractor`` over trafilatura: a page's main text as Markdown with images and links."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import trafilatura

from jesinia.services.fetch.application.ports import Article

# A Markdown link or image: ``[text](target)`` or ``[text](<target>)``, optionally ``!``-led.
_LINK = re.compile(
    r"(?P<bang>!?)\[(?P<text>[^\]]*)\]\((?:<(?P<angled>[^>]*)>|(?P<plain>[^)\s]*))\)"
)


def _is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _keep_web_targets(markdown: str) -> tuple[str, list[str]]:
    """The Markdown with every non-http(s) link reduced to its text and every non-http(s)
    image dropped (the text is from a remote page: no ``javascript:`` or ``data:`` targets),
    plus the image URLs that remain, in order and without repeats."""
    images: list[str] = []

    def rewrite(match: re.Match[str]) -> str:
        target = match["angled"] if match["angled"] is not None else match["plain"]
        if _is_web_url(target):
            if match["bang"] and target not in images:
                images.append(target)
            return match[0]
        return "" if match["bang"] else match["text"]

    return _LINK.sub(rewrite, markdown), images


class TrafilaturaExtractor:
    def extract(self, html: bytes, url: str) -> Article | None:
        tree = trafilatura.load_html(html)
        if tree is None:
            return None
        # Metadata first: extraction prunes the tree it is given.
        title = trafilatura.extract_metadata(tree, default_url=url).title
        markdown = trafilatura.extract(
            tree,
            url=url,
            output_format="markdown",
            include_images=True,
            include_links=True,
            include_comments=False,
        )
        if not markdown:
            return None
        text, images = _keep_web_targets(markdown)
        return Article(title or None, text, images)
