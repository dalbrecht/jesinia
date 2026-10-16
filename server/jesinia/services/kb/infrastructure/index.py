# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page index and link graph in ``kb.sqlite`` (derived; rebuilt by a scan)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import PurePosixPath

from jesinia.core.db import transaction
from jesinia.services.kb.application.ports import IndexRow
from jesinia.services.kb.domain.page import PageId

_COLS = "id, path, disk, rev, title, mtime"


def _row(r: tuple[str, str, str, str, str, float]) -> IndexRow:
    return IndexRow(PageId(r[0]), PurePosixPath(r[1]), r[2], r[3], r[4], r[5])


class SqliteIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, id: PageId) -> IndexRow | None:
        r = self.conn.execute(f"SELECT {_COLS} FROM pages WHERE id = ?", (id,)).fetchone()
        return _row(r) if r else None

    def by_path(self, path: PurePosixPath) -> IndexRow | None:
        r = self.conn.execute(f"SELECT {_COLS} FROM pages WHERE path = ?", (str(path),)).fetchone()
        return _row(r) if r else None

    def put(
        self, id: PageId, path: PurePosixPath, disk: str, rev: str, title: str, mtime: float
    ) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM pages WHERE path = ? AND id != ?", (str(path), id))
            self.conn.execute(
                "INSERT INTO pages (id, path, disk, rev, title, mtime) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET path=excluded.path, disk=excluded.disk, "
                "rev=excluded.rev, title=excluded.title, mtime=excluded.mtime",
                (id, str(path), disk, rev, title, mtime),
            )

    def delete(self, id: PageId) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM pages WHERE id = ?", (id,))
            self.conn.execute("DELETE FROM links WHERE src = ?", (id,))

    def all(self) -> list[IndexRow]:
        return [_row(r) for r in self.conn.execute(f"SELECT {_COLS} FROM pages ORDER BY path")]

    def set_links(self, src: PageId, dsts: Iterable[PageId]) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM links WHERE src = ?", (src,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO links (src, dst) VALUES (?, ?)", [(src, d) for d in dsts]
            )

    def links_to(self, dst: PageId) -> list[PageId]:
        rows = self.conn.execute("SELECT src FROM links WHERE dst = ? ORDER BY src", (dst,))
        return [PageId(r[0]) for r in rows]
