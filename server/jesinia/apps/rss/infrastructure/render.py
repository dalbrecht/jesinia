# SPDX-License-Identifier: AGPL-3.0-or-later
"""A cached full article (Markdown) as HTML for Reader API clients: CommonMark with raw
HTML disabled, then the same allowlist as feed content."""

from __future__ import annotations

from markdown_it import MarkdownIt

from jesinia.services.fetch.api.content import sanitize_html


class MarkdownArticleRenderer:
    def __init__(self) -> None:
        # Raw HTML in the Markdown is shown as text, never passed through; tables render.
        self._md = MarkdownIt("commonmark", {"html": False}).enable("table")

    def render(self, markdown: str, base_url: str) -> str:
        return sanitize_html(self._md.render(markdown), base_url)
