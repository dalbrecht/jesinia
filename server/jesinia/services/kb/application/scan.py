# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reconcile the index with the KB folder (spec §5.4).

- Pages are matched by ``id`` (NFC-normalized paths), so external renames are moves.
- Plain ``.md`` files are adopted: an id is minted and front-matter written (never in
  reserved, hidden, or ``_assets`` paths, and never through a symlink).
- Foreign front-matter (no ``id``) is never rewritten.
- Vanished pages and duplicate ids are reported as conflicts; nothing is deleted.
- Unchanged files (same mtime as indexed) are not re-read, so rescans stay cheap.
- One unreadable or malformed file is skipped and reported; it never stops the scan.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from jesinia.services.kb.application.errors import KbError
from jesinia.services.kb.application.service import Change, KbService, nfc
from jesinia.services.kb.domain.frontmatter import page_from, parse_document, serialize_page
from jesinia.services.kb.domain.links import link_targets
from jesinia.services.kb.domain.page import Page, PageId, new_page_id
from jesinia.services.kb.domain.paths import is_page_candidate, is_reserved

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DuplicateId:
    id: PageId
    paths: list[PurePosixPath]


@dataclass(frozen=True)
class ScanReport:
    indexed: int
    adopted: list[PurePosixPath] = field(default_factory=list)
    missing: list[PageId] = field(default_factory=list)
    duplicates: list[DuplicateId] = field(default_factory=list)
    foreign_skipped: int = 0
    errors: list[tuple[PurePosixPath, str]] = field(default_factory=list)  # skipped files


@dataclass(frozen=True)
class _Found:
    id: PageId
    path: PurePosixPath
    disk: str
    rev: str
    title: str
    mtime: float
    body: str | None  # None when reused from the index (unchanged mtime)


Visit = Literal["page", "foreign", "skipped"]


def _visit(
    svc: KbService,
    disk: str,
    rel: PurePosixPath,
    by_id: dict[PageId, list[_Found]],
    adopted: list[PurePosixPath],
) -> Visit:
    fs = svc.files
    assert fs is not None
    index = svc.index
    mtime = (fs.root / disk).stat().st_mtime
    known = index.by_path(rel)
    if known is not None and known.disk == disk and known.mtime == mtime:
        by_id[known.id].append(_Found(known.id, rel, disk, known.rev, known.title, mtime, None))
        return "page"
    data = fs.read(disk)
    doc = parse_document(data.decode("utf-8", errors="replace"))
    if doc.kind == "foreign":
        return "foreign"
    if doc.kind == "plain":
        if is_reserved(rel):
            return "skipped"
        if fs.read(disk) != data:
            return "skipped"  # changed under us; the next scan will adopt it
        page = Page(new_page_id(), rel.stem, rel, doc.body, {}, {})
        data = serialize_page(page).encode()
        mtime = fs.write_atomic(disk, data)
        adopted.append(rel)
        doc = parse_document(data.decode("utf-8"))
    page = page_from(doc, rel)
    rev = hashlib.sha256(data).hexdigest()
    by_id[page.id].append(_Found(page.id, rel, disk, rev, page.title, mtime, page.body))
    return "page"


async def scan(svc: KbService) -> ScanReport:
    fs = svc.files
    if fs is None:
        return ScanReport(indexed=0)
    index = svc.index
    by_id: dict[PageId, list[_Found]] = defaultdict(list)
    adopted: list[PurePosixPath] = []
    errors: list[tuple[PurePosixPath, str]] = []
    foreign = 0

    for disk in fs.walk():
        rel = nfc(disk)
        if not is_page_candidate(rel):
            continue
        try:
            if _visit(svc, disk, rel, by_id, adopted) == "foreign":
                foreign += 1
        except (OSError, KbError, ValueError) as exc:
            log.warning("scan skipped %s: %s", rel, exc)
            errors.append((rel, str(exc)))

    duplicates: dict[PageId, list[PurePosixPath]] = {}
    seen: set[PageId] = set()
    for id, hits in by_id.items():
        previous = index.get(id)
        if len(hits) > 1:
            duplicates[id] = sorted(h.path for h in hits)
        keep = next((h for h in hits if previous and h.path == previous.path), hits[0])
        seen.add(id)
        index.put(id, keep.path, keep.disk, keep.rev, keep.title, keep.mtime)
        if keep.body is not None:
            index.set_links(id, link_targets(keep.body))
        change: Change
        if previous is None:
            change = "created"
        elif previous.path != keep.path:
            change = "moved"
        elif previous.rev != keep.rev:
            change = "updated"
        else:
            continue
        if keep.path in adopted:
            continue  # adoption is reported via the scan report, not as an external edit
        await svc.emit(id, keep.path, change, "external")

    missing: list[PageId] = []
    for row in index.all():
        if row.id not in seen:
            missing.append(row.id)
            index.add_missing(row.id, row.path)
            index.delete(row.id)
            await svc.emit(row.id, row.path, "missing", "external")
    index.clear_missing(seen)
    index.set_duplicates(duplicates)
    return ScanReport(
        indexed=len(seen),
        adopted=sorted(adopted),
        missing=sorted(missing),
        duplicates=[DuplicateId(i, p) for i, p in sorted(duplicates.items())],
        foreign_skipped=foreign,
        errors=errors,
    )
