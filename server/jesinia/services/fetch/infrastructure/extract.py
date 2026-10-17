# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ArticleExtractor`` over trafilatura: a page's main text as Markdown with images and links.

The page goes through the allowlist sanitiser before trafilatura sees it, so every link and
image it can emit already has an absolute http(s) URL (spec §6). Nothing parses the Markdown
afterwards.
"""

from __future__ import annotations

import lxml.html
import trafilatura

from jesinia.services.fetch.application.ports import Article
from jesinia.services.fetch.infrastructure.sanitize import sanitize_page


def _clean_tree(raw: lxml.html.HtmlElement, url: str) -> lxml.html.HtmlElement:
    """The page, sanitised, as a tree; a link or image that lost its URL is reduced to its
    text (a bare ``<a>`` would become a ``[text]`` reference a renderer could resolve)."""
    page = sanitize_page(lxml.html.tostring(raw, encoding="unicode"), url)
    tree = lxml.html.document_fromstring(f"<html><body>{page}</body></html>")
    for anchor in list(tree.iter("a")):
        if not anchor.get("href"):
            anchor.drop_tag()
    for image in list(tree.iter("img")):
        if not image.get("src"):
            image.drop_tree()
    return tree


class TrafilaturaExtractor:
    def extract(self, html: bytes, url: str) -> Article | None:
        raw = trafilatura.load_html(html)
        if raw is None:
            return None
        # The title comes from the raw page's metadata (the sanitiser drops <head>).
        title = trafilatura.extract_metadata(raw, default_url=url).title
        tree = _clean_tree(raw, url)
        # Collected before extraction, which prunes the tree it is given.
        sources = [str(image.get("src")) for image in tree.iter("img")]
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
        images = list(dict.fromkeys(s for s in sources if s in markdown))
        return Article(title or None, markdown, images)
