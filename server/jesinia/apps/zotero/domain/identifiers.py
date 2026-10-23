# SPDX-License-Identifier: AGPL-3.0-or-later
"""Item keys, paper identifiers (arXiv id, DOI) and zotero:// links; regexes only."""

from __future__ import annotations

import re
from typing import NewType

ItemKey = NewType("ItemKey", str)

_NEW_ARXIV = r"\d{4}\.\d{4,5}"
# Pre-2007 arXiv archives; a generic [a-z]+ prefix would match URLs like /stable/1234567.
OLD_ARXIV_ARCHIVES = (
    "astro-ph|cond-mat|gr-qc|hep-ex|hep-lat|hep-ph|hep-th|math-ph|nlin|nucl-ex|nucl-th"
    "|physics|quant-ph|math|cs|q-bio|q-fin|stat|alg-geom|chao-dyn|cmp-lg|dg-ga|funct-an"
    "|patt-sol|solv-int|adap-org|comp-gas|supr-con|acc-phys|ao-sci|atom-ph|bayes-an"
    "|chem-ph|plasm-ph|mtrl-th"
)
_OLD_ARXIV = rf"(?:{OLD_ARXIV_ARCHIVES})(?:\.[A-Z]{{2}})?/\d{{7}}"
_ARXIV_ID = re.compile(rf"(?<![\w.-])(?:({_NEW_ARXIV})|({_OLD_ARXIV}))(?:v\d+)?(?!\d)")
_BARE_ARXIV_TITLE = re.compile(rf"(?:arxiv:)?(?:{_NEW_ARXIV})(?:v\d+)?", re.IGNORECASE)
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
_TRAILING_PUNCTUATION = ".,;:)]}'\""


def find_arxiv_id(*texts: str) -> str | None:
    """Return the first arXiv id found in ``texts``, without its version suffix."""
    for text in texts:
        match = _ARXIV_ID.search(text)
        if match:
            return match.group(1) or match.group(2)
    return None


def find_doi(*texts: str) -> str | None:
    """Return the first DOI found in ``texts``, lowercased and without a resolver prefix."""
    for text in texts:
        match = _DOI.search(text)
        if match:
            return match.group(0).rstrip(_TRAILING_PUNCTUATION).lower()
    return None


def title_needs_fix(title: str) -> bool:
    """Whether a title is a placeholder (empty, bare arXiv id, URL or file name)."""
    stripped = title.strip()
    if not stripped:
        return True
    unwrapped = stripped[1:-1].strip() if stripped[0] == "[" and stripped[-1] == "]" else stripped
    return bool(
        _BARE_ARXIV_TITLE.fullmatch(unwrapped)
        or re.match(r"https?://", stripped, re.IGNORECASE)
        or stripped.lower().endswith(".pdf")
    )


def zotero_select_url(key: ItemKey) -> str:
    """Link that selects an item in the Zotero desktop app."""
    return f"zotero://select/library/items/{key}"


def zotero_annotation_url(attachment: ItemKey, annotation: ItemKey, page_label: str) -> str:
    """Link that opens a PDF attachment at an annotation (page omitted when unknown)."""
    page = f"page={page_label}&" if page_label else ""
    return f"zotero://open-pdf/library/items/{attachment}?{page}annotation={annotation}"
