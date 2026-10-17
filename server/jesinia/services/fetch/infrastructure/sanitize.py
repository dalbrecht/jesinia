# SPDX-License-Identifier: AGPL-3.0-or-later
"""Make fetched HTML safe to render: an allowlist via ``nh3`` with absolute URLs."""

from __future__ import annotations

from urllib.parse import urljoin

import nh3

TAGS = frozenset(
    {
        *(f"h{n}" for n in range(1, 7)),
        "p", "br", "hr", "a", "img", "ul", "ol", "li", "dl", "dt", "dd", "blockquote",
        "pre", "code", "em", "strong", "b", "i", "s", "sub", "sup", "figure", "figcaption",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    }
)  # fmt: skip
ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}
URL_ATTRIBUTES = frozenset({"href", "src"})
# Page structure article extraction reads to tell the article from navigation and
# boilerplate (tags and class/id only: nothing that can carry a URL or script).
STRUCTURE_TAGS = frozenset(
    {"article", "main", "section", "div", "span", "nav", "header", "footer", "aside"}
)
STRUCTURE_ATTRIBUTES = frozenset({"class", "id"})
# Dropped with their text, not just unwrapped: their content is never prose.
_DROPPED_WITH_CONTENT = {"script", "style", "title", "noscript", "template"}


def _clean(
    html: str,
    base_url: str,
    *,
    tags: frozenset[str],
    attributes: dict[str, set[str]],
    schemes: set[str],
) -> str:
    def absolute(tag: str, attribute: str, value: str) -> str | None:
        return urljoin(base_url, value) if attribute in URL_ATTRIBUTES else value

    return nh3.clean(
        html,
        tags=set(tags),
        clean_content_tags=_DROPPED_WITH_CONTENT,
        attributes=attributes,
        attribute_filter=absolute,
        url_schemes=schemes,
        link_rel="noopener noreferrer",
    )


def sanitize_html(html: str, base_url: str) -> str:
    """Feed content as safe HTML: the allowlist, with absolute http(s)/mailto URLs."""
    return _clean(
        html,
        base_url,
        tags=TAGS,
        attributes={k: set(v) for k, v in ATTRIBUTES.items()},
        schemes={"http", "https", "mailto"},
    )


def sanitize_page(html: str, base_url: str) -> str:
    """A whole page through the same allowlist before article extraction, plus the
    structural tags extraction needs. Links and images keep absolute http(s) URLs only;
    any other ``href`` or ``src`` (``javascript:``, ``data:``, ``mailto:``) is removed."""
    attributes = {k: set(v) for k, v in ATTRIBUTES.items()}
    attributes["*"] = set(STRUCTURE_ATTRIBUTES)
    return _clean(
        html, base_url, tags=TAGS | STRUCTURE_TAGS, attributes=attributes, schemes={"http", "https"}
    )
