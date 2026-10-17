# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ArticleExtractor`` over trafilatura: a page's main text as Markdown with images and links.

The page goes through the allowlist sanitiser before trafilatura sees it, so every link and
image it can emit already has an absolute http(s) URL (spec §6). Before extraction the
sanitised tree loses site clutter and its headings are made plain text; after it the Markdown
gets a line-based repair pass (ruling R-REN1) that never adds or changes a link target.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag

import lxml.html
import trafilatura

from jesinia.services.fetch.application.ports import Article
from jesinia.services.fetch.domain.markdown import normalize_markdown
from jesinia.services.fetch.infrastructure.sanitize import sanitize_page

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
CLUTTER_TAGS = ("nav", "aside", "footer")
CLUTTER_ATTRIBUTES = ("class", "id", "aria-label")
# Only structural containers can be clutter; inline elements (a highlighter's
# ``<span class="comment">``) and anything inside code never are.
CLUTTER_CONTAINER_TAGS = frozenset(
    {"div", "section", "aside", "nav", "footer", "header", "ul", "ol", "dl", "p", "table"}
)
CODE_TAGS = ("pre", "code")
# Words in a class, id or aria-label that mark site furniture. A word must match exactly, so
# "commentary", "shared-content" and "navy" are kept.
CLUTTER_WORDS = frozenset(
    {
        "related", "recent", "sidebar", "comment", "comments", "share", "sharing", "subscribe",
        "newsletter", "footer", "nav", "breadcrumb", "breadcrumbs",
    }
)  # fmt: skip
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_SEPARATOR = re.compile(r"[-_\s]+")
# An element holding more than this share of the page's text is the article, whatever its
# class says (``<article class="post recent">``), and is never dropped as clutter.
ARTICLE_TEXT_SHARE = 0.5
_WHITESPACE = re.compile(r"\s+")
# Stands in for an empty line inside a code block during extraction: trafilatura drops empty
# lines from code, and this visible symbol (U+2400) is one it keeps. Used only on pages that
# do not already contain it.
BLANK_CODE_LINE = "\u2400"


def _text_length(element: lxml.html.HtmlElement) -> int:
    return sum(len(part) for part in str(element.text_content()).split())


def _words(name: str | None) -> set[str]:
    """The lower-case words of a class, id or label: split on ``-``, ``_``, whitespace and
    camelCase boundaries (``recentArticles`` is "recent" and "articles")."""
    spaced = _CAMEL_CASE_BOUNDARY.sub(" ", name or "")
    return {word.lower() for word in _WORD_SEPARATOR.split(spaced) if word}


def _is_inside_code(element: lxml.html.HtmlElement) -> bool:
    return next(element.iterancestors(*CODE_TAGS), None) is not None


def _is_clutter(element: lxml.html.HtmlElement) -> bool:
    if element.tag not in CLUTTER_CONTAINER_TAGS or _is_inside_code(element):
        return False
    if element.tag in CLUTTER_TAGS:
        return True
    return any(
        not CLUTTER_WORDS.isdisjoint(_words(element.get(name))) for name in CLUTTER_ATTRIBUTES
    )


def _drop_clutter(tree: lxml.html.HtmlElement) -> None:
    """Drop navigation, sidebars, footers and "recent/related" lists, keeping the article."""
    body = tree.find("body")
    page_text = _text_length(body)
    for element in list(body.iterdescendants()):
        if not _is_clutter(element):
            continue
        if _text_length(element) <= ARTICLE_TEXT_SHARE * page_text:
            element.drop_tree()


def _collapse(text: str | None) -> str | None:
    return None if text is None else _WHITESPACE.sub(" ", text)


def _is_self_anchor(href: str, page_url: str) -> bool:
    """A link to a place on this page (``#part`` or the page's own URL with a fragment)."""
    return href.startswith("#") or urldefrag(href).url == urldefrag(page_url).url


def _text_slots(element: lxml.html.HtmlElement) -> list[tuple[lxml.html.HtmlElement, str]]:
    """Where ``element``'s text lives, in reading order: (node, "text" or "tail") pairs."""
    slots = [(element, "text")]
    for child in element:
        slots.extend(_text_slots(child))
        slots.append((child, "tail"))
    return slots


def _trim_ends(slots: list[tuple[lxml.html.HtmlElement, str]], *, from_start: bool) -> None:
    """Strip whitespace from one end of the text in ``slots`` up to the first visible text."""
    for node, name in slots if from_start else reversed(slots):
        value = getattr(node, name)
        if value is None:
            continue
        value = value.lstrip() if from_start else value.rstrip()
        setattr(node, name, value)
        if value:
            return


def _flatten_headings(tree: lxml.html.HtmlElement, page_url: str) -> None:
    """Headings as one line of text: self-anchor links unwrapped (other links kept) and
    whitespace collapsed and trimmed (a line break inside a heading splits it into an empty
    heading and stray text)."""
    for heading in list(tree.iter(*HEADING_TAGS)):
        for anchor in list(heading.iter("a")):
            if _is_self_anchor(str(anchor.get("href")), page_url):
                anchor.drop_tag()
        slots = _text_slots(heading)
        for node, name in slots:
            setattr(node, name, _collapse(getattr(node, name)))
        _trim_ends(slots, from_start=True)
        _trim_ends(slots, from_start=False)


def _prepare_preformatted_text(tree: lxml.html.HtmlElement) -> None:
    """Code made safe for the extractor. Everything inside a ``<pre>`` loses its class, id
    and label. A ``<pre>`` with no ``<code>`` inside (syntax highlighting as bare
    ``<span>``s) holds its text in a single ``<code>``: the extractor only fences
    ``pre > code``, and otherwise renders the lines as prose, where a ``# comment`` becomes a
    heading. The spans are dropped; their text, whitespace and line breaks (``<br>``
    included) are kept exactly."""
    for block in list(tree.iter("pre")):
        # Class names inside code are a highlighter's, not page structure; the extractor's
        # own boilerplate rules would prune a ``<span class="comment">``.
        for node in block.iter():
            for name in CLUTTER_ATTRIBUTES:
                node.attrib.pop(name, None)
        if block.find(".//code") is not None:
            continue
        for line_break in block.iter("br"):
            line_break.tail = "\n" + (line_break.tail or "")
        text = str(block.text_content())
        for child in list(block):
            block.remove(child)
        block.text = None
        code = lxml.html.Element("code")
        code.text = text
        block.append(code)


def _mark_blank_lines(text: str | None) -> str | None:
    """``BLANK_CODE_LINE`` appended to every empty or whitespace-only line of ``text`` that
    lies between two of its line breaks (the first and last pieces may continue a line from
    a neighbouring node)."""
    if text is None:
        return None
    lines = text.split("\n")
    for index in range(1, len(lines) - 1):
        if not lines[index].strip():
            lines[index] += BLANK_CODE_LINE
    return "\n".join(lines)


def _mark_blank_code_lines(tree: lxml.html.HtmlElement) -> bool:
    """Put ``BLANK_CODE_LINE`` on every empty or whitespace-only line inside a ``<pre>``;
    False (and nothing changed) when the page already contains the symbol."""
    if BLANK_CODE_LINE in str(tree.text_content()):
        return False
    for block in tree.iter("pre"):
        for node in block.iter():
            node.text = _mark_blank_lines(node.text)
            if node is not block:
                node.tail = _mark_blank_lines(node.tail)
    return True


def _restore_blank_code_lines(markdown: str) -> str:
    """The marked lines back as they were: empty, or only their own whitespace."""
    lines = markdown.split("\n")
    return "\n".join(
        line.replace(BLANK_CODE_LINE, "") if line.strip() == BLANK_CODE_LINE else line
        for line in lines
    )


def _trim_block_tails(tree: lxml.html.HtmlElement) -> None:
    """Source indentation after a ``<pre>`` is dropped: the extractor copies a code block's
    whitespace-only tail verbatim, which indents the next paragraph into a code block."""
    for block in tree.iter("pre"):
        if block.tail is not None and not block.tail.strip():
            block.tail = "\n"


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
    _flatten_headings(tree, url)
    _prepare_preformatted_text(tree)
    _trim_block_tails(tree)
    return tree


class TrafilaturaExtractor:
    def extract(self, html: bytes, url: str) -> Article | None:
        raw = trafilatura.load_html(html)
        if raw is None:
            return None
        # The title comes from the raw page's metadata (the sanitiser drops <head>).
        title = trafilatura.extract_metadata(raw, default_url=url).title
        tree = _clean_tree(raw, url)
        has_marked_blank_lines = _mark_blank_code_lines(tree)
        # Collected before extraction, which prunes the tree it is given.
        sources = [str(image.get("src")) for image in tree.iter("img")]
        markdown = trafilatura.extract(
            tree,
            url=url,
            output_format="markdown",
            include_images=True,
            include_links=True,
            include_comments=False,
            # Not favor_precision: it drops headings that are a single link as link-dense
            # boilerplate; site clutter is removed by _drop_clutter instead.
        )
        if not markdown:
            return None
        if has_marked_blank_lines:
            markdown = _restore_blank_code_lines(markdown)
        markdown = normalize_markdown(markdown)
        images = list(dict.fromkeys(s for s in sources if s in markdown))
        return Article(title or None, markdown, images)
