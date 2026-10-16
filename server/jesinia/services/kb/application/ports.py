# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the KB service needs from the outside world; implemented in ``infrastructure``."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from jesinia.services.kb.domain.page import PageId


@dataclass(frozen=True)
class IndexRow:
    id: PageId
    path: PurePosixPath  # NFC logical path
    disk: str  # the path as found on disk
    rev: str
    title: str
    mtime: float


@dataclass(frozen=True)
class Conflict:
    kind: str  # "duplicate_id" | "missing"
    id: PageId
    paths: list[PurePosixPath]
    detected_at: str


class PageFiles(Protocol):
    root: Path

    def contain(self, rel: PurePosixPath) -> Path:
        """Absolute path for ``rel``; raises ``PathOutsideKb`` if it escapes the root."""
        ...

    def read(self, disk: str) -> bytes: ...

    def write_atomic(
        self, disk: str, data: bytes, *, expected_rev: str | None = None, create: bool = False
    ) -> float:
        """Write via temp file + fsync + rename; return the new mtime. ``create`` never
        replaces an existing file; ``expected_rev`` is re-checked just before the replace
        (``StaleWrite`` if the file changed)."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Move a file; never replaces an existing ``dst`` (``FileExistsError``)."""
        ...

    def exists(self, disk: str) -> bool: ...

    def walk(self) -> Iterator[str]:
        """Every file below the root, as root-relative POSIX strings."""
        ...


class PageIndex(Protocol):
    def get(self, id: PageId) -> IndexRow | None: ...

    def by_path(self, path: PurePosixPath) -> IndexRow | None: ...

    def put(
        self,
        id: PageId,
        path: PurePosixPath,
        disk: str,
        rev: str,
        title: str,
        mtime: float,
        links: Iterable[PageId] | None = None,
        tags: Iterable[str] | None = None,
        source: str | None = None,
    ) -> None:
        """Upsert a page row; with ``links``, replace its outbound links atomically.

        With ``tags``, also replace its tags and its ``source`` (``None`` clears it).
        """
        ...

    def delete(self, id: PageId) -> None: ...

    def all(self) -> list[IndexRow]: ...

    def set_links(self, src: PageId, dsts: Iterable[PageId]) -> None: ...

    def set_tags(self, id: PageId, tags: Iterable[str]) -> None: ...

    def set_source(self, id: PageId, url: str | None) -> None: ...

    def tag_counts(self) -> dict[str, int]:
        """How many pages carry each tag."""
        ...

    def pages_with_tag(self, tag: str) -> list[PageId]: ...

    def by_source(self, url: str) -> PageId | None: ...

    def reset(self) -> None:
        """Forget everything (a different KB root)."""
        ...

    def indexed_root(self) -> str | None:
        """The KB root the index was built from (persisted across restarts)."""
        ...

    def set_indexed_root(self, root: str) -> None: ...

    def links_to(self, dst: PageId) -> list[PageId]: ...

    def set_duplicates(self, dups: dict[PageId, list[PurePosixPath]]) -> None:
        """Replace all ``duplicate_id`` conflicts."""
        ...

    def add_missing(self, id: PageId, path: PurePosixPath) -> None: ...

    def clear_missing(self, ids: Iterable[PageId]) -> None: ...

    def conflicts(self) -> list[Conflict]: ...

    def stat(self, path: PurePosixPath) -> float | None:
        """Last indexed mtime for ``path`` (NFC), for incremental scans."""
        ...
