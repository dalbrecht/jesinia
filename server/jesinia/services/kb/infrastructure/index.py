# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page index and link graph in ``kb.sqlite`` (derived; rebuilt by a scan)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import PurePosixPath

from jesinia.core.db import transaction
from jesinia.services.kb.application.ports import Conflict, IndexRow
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

    def reset(self) -> None:
        with transaction(self.conn):
            for table in ("pages", "links", "conflicts"):
                self.conn.execute(f"DELETE FROM {table}")

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

    def set_duplicates(self, dups: dict[PageId, list[PurePosixPath]]) -> None:
        now = datetime.now(UTC).isoformat()
        with transaction(self.conn):
            self.conn.execute("DELETE FROM conflicts WHERE kind = 'duplicate_id'")
            self.conn.executemany(
                "INSERT INTO conflicts (kind, page_id, paths_json, detected_at) "
                "VALUES ('duplicate_id', ?, ?, ?)",
                [(id, json.dumps([str(p) for p in paths]), now) for id, paths in dups.items()],
            )

    def add_missing(self, id: PageId, path: PurePosixPath) -> None:
        now = datetime.now(UTC).isoformat()
        with transaction(self.conn):
            self.conn.execute("DELETE FROM conflicts WHERE kind = 'missing' AND page_id = ?", (id,))
            self.conn.execute(
                "INSERT INTO conflicts (kind, page_id, paths_json, detected_at) "
                "VALUES ('missing', ?, ?, ?)",
                (id, json.dumps([str(path)]), now),
            )

    def clear_missing(self, ids: Iterable[PageId]) -> None:
        with transaction(self.conn):
            self.conn.executemany(
                "DELETE FROM conflicts WHERE kind = 'missing' AND page_id = ?", [(i,) for i in ids]
            )

    def conflicts(self) -> list[Conflict]:
        rows = self.conn.execute(
            "SELECT kind, page_id, paths_json, detected_at FROM conflicts ORDER BY kind, page_id"
        )
        return [
            Conflict(k, PageId(i), [PurePosixPath(p) for p in json.loads(pj)], at)
            for k, i, pj, at in rows
        ]

    def stat(self, path: PurePosixPath) -> float | None:
        r = self.conn.execute("SELECT mtime FROM pages WHERE path = ?", (str(path),)).fetchone()
        return float(r[0]) if r else None
