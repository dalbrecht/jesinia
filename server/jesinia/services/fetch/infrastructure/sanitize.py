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


def sanitize_html(html: str, base_url: str) -> str:
    def absolute(tag: str, attribute: str, value: str) -> str | None:
        return urljoin(base_url, value) if attribute in URL_ATTRIBUTES else value

    return nh3.clean(
        html,
        tags=set(TAGS),
        attributes={k: set(v) for k, v in ATTRIBUTES.items()},
        attribute_filter=absolute,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )
