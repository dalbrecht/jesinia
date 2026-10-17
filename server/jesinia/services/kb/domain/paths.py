# SPDX-License-Identifier: AGPL-3.0-or-later
"""Title-as-path rules (spec §5.1, §5.4)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath

from jesinia.services.kb.domain.page import PageId

RESERVED_TOP = frozenset({"journal", "drafts"})
ASSETS_DIR = "_assets"
_WS = re.compile(r"\s+")


def sanitize_title(title: str) -> str:
    """NFC; path separators, ``:`` and control chars → space; collapse; trim dots at both
    ends (a leading one would make a hidden file the tree and index skip)."""
    text = unicodedata.normalize("NFC", title)
    text = "".join(" " if ch in "/\\:" or unicodedata.category(ch) == "Cc" else ch for ch in text)
    text = _WS.sub(" ", text).strip(". ")
    return text or "Untitled"


def filename_for(title: str, id: PageId, siblings: Iterable[tuple[str, PageId]]) -> str:
    """``<title>.md``, or ``<title> [<8 hex>].md`` when a same-titled sibling sorts first."""
    name = sanitize_title(title)
    key = name.casefold()
    rivals = [sid for st, sid in siblings if sid != id and sanitize_title(st).casefold() == key]
    if not rivals or id < min(rivals):
        return f"{name}.md"
    return f"{name} [{id.replace('-', '')[:8]}].md"


def is_hidden(rel: PurePosixPath) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def is_reserved(rel: PurePosixPath) -> bool:
    return bool(rel.parts) and rel.parts[0].casefold() in RESERVED_TOP


def is_page_candidate(rel: PurePosixPath) -> bool:
    return rel.suffix.lower() == ".md" and not is_hidden(rel) and ASSETS_DIR not in rel.parts
