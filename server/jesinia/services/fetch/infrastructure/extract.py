# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ArticleExtractor`` over trafilatura: a page's main text as Markdown with images and links.

The page goes through the allowlist sanitiser before trafilatura sees it, so every link and
image it can emit already has an absolute http(s) URL (spec §6). Before extraction the
sanitised tree loses site clutter and its headings are made plain text; after it the Markdown
gets a line-based repair pass (ruling R-REN1) that never adds or changes a link target.
"""

from __future__ import annotations

import re

import lxml.html
import trafilatura

from jesinia.services.fetch.application.ports import Article
from jesinia.services.fetch.domain.markdown import normalize_markdown
from jesinia.services.fetch.infrastructure.sanitize import sanitize_page

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
CLUTTER_TAGS = ("nav", "aside", "footer")
CLUTTER_ATTRIBUTES = ("class", "id", "aria-label")
# A name part that marks site furniture; it must start a word, so "canvas" is not "nav".
CLUTTER_NAME = re.compile(
    r"(?<![a-z0-9])(related|recent|sidebar|comments?|share|subscribe|newsletter|footer|nav"
    r"|breadcrumb)",
    re.IGNORECASE,
)
# An element holding more than this share of the page's text is the article, whatever its
# class says (``<article class="post recent">``), and is never dropped as clutter.
ARTICLE_TEXT_SHARE = 0.5
_WHITESPACE = re.compile(r"\s+")


def _text_length(element: lxml.html.HtmlElement) -> int:
    return sum(len(part) for part in str(element.text_content()).split())


def _is_clutter(element: lxml.html.HtmlElement) -> bool:
    if element.tag in CLUTTER_TAGS:
        return True
    return any(CLUTTER_NAME.search(element.get(name) or "") for name in CLUTTER_ATTRIBUTES)


def _drop_clutter(tree: lxml.html.HtmlElement) -> None:
    """Drop navigation, sidebars, footers and "recent/related" lists, keeping the article."""
    body = tree.find("body")
    page_text = _text_length(body)
    for element in list(body.iterdescendants()):
        if not isinstance(element.tag, str) or not _is_clutter(element):
            continue
        if _text_length(element) <= ARTICLE_TEXT_SHARE * page_text:
            element.drop_tree()


def _collapse(text: str | None) -> str | None:
    return None if text is None else _WHITESPACE.sub(" ", text)


def _flatten_headings(tree: lxml.html.HtmlElement) -> None:
    """Headings as one line of text: self-anchor links unwrapped, whitespace collapsed (a
    link or a line break inside a heading splits it into an empty heading and stray text)."""
    for heading in list(tree.iter(*HEADING_TAGS)):
        for anchor in list(heading.iter("a")):
            anchor.drop_tag()
        heading.text = _collapse(heading.text)
        for child in heading.iterdescendants():
            child.text = _collapse(child.text)
            child.tail = _collapse(child.tail)
        if heading.text:
            heading.text = heading.text.lstrip()
        last = heading[-1] if len(heading) else None
        if last is not None and last.tail:
            last.tail = last.tail.rstrip()
        elif last is None and heading.text:
            heading.text = heading.text.rstrip()


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
    _drop_clutter(tree)
    _flatten_headings(tree)
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
            favor_precision=True,
        )
        if not markdown:
            return None
        markdown = normalize_markdown(markdown)
        images = list(dict.fromkeys(s for s in sources if s in markdown))
        return Article(title or None, markdown, images)
