# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-side value objects describing a Zotero paper and its children."""

from __future__ import annotations

from dataclasses import dataclass

from jesinia.apps.zotero.domain.identifiers import ItemKey


@dataclass(frozen=True)
class Creator:
    """An author, editor or other contributor."""

    first: str
    last: str
    kind: str


@dataclass(frozen=True)
class ZoteroTag:
    """A Zotero tag; ``automatic`` tags were added by Zotero, not the user."""

    name: str
    automatic: bool


@dataclass(frozen=True)
class Annotation:
    """A PDF highlight or comment."""

    key: ItemKey
    text: str
    comment: str
    page_label: str
    color: str


@dataclass(frozen=True)
class Attachment:
    """A file or link attached to a paper, with any PDF annotations."""

    key: ItemKey
    title: str
    content_type: str
    filename: str
    url: str
    annotations: tuple[Annotation, ...]


@dataclass(frozen=True)
class Note:
    """A child note; ``html`` is Zotero's native note markup."""

    key: ItemKey
    html: str


@dataclass(frozen=True)
class Paper:
    """A bibliographic item. When ``standalone`` is true it is a parentless PDF: ``key`` is the
    attachment key and ``attachments`` holds just that PDF."""

    key: ItemKey
    version: int
    item_type: str
    title: str
    creators: tuple[Creator, ...]
    date: str
    abstract: str
    doi: str
    url: str
    extra: str
    tags: tuple[ZoteroTag, ...]
    related: tuple[ItemKey, ...]
    notes: tuple[Note, ...]
    attachments: tuple[Attachment, ...]
    standalone: bool
