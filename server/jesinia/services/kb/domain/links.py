# SPDX-License-Identifier: AGPL-3.0-or-later
"""Internal links ``[[id|Title]]`` / ``[[id]]`` (spec §5.3). Links resolve by id."""

from __future__ import annotations

import re

from jesinia.services.kb.domain.page import PageId

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_LINK = re.compile(rf"\[\[({_UUID})(?:\|([^\]]*))?\]\]")


def link_targets(body: str) -> set[PageId]:
    return {PageId(m.group(1)) for m in _LINK.finditer(body)}


def retitle_links(body: str, id: PageId, title: str) -> str:
    """Update the title text of ``[[id|…]]`` links to ``id``; bare ``[[id]]`` stays."""
    safe = " ".join(title.replace("|", " ").replace("]", " ").split())

    def sub(m: re.Match[str]) -> str:
        if m.group(1) != id or m.group(2) is None:
            return m.group(0)
        return f"[[{id}|{safe}]]"

    return _LINK.sub(sub, body)
