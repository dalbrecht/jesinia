# SPDX-License-Identifier: AGPL-3.0-or-later
"""``fetch.sqlite`` (cache class): bodies and validators for conditional re-fetches."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from jesinia.core.db import transaction
from jesinia.services.fetch.application.ports import CachedFetch


class SqliteFetchCache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, url: str) -> CachedFetch | None:
        r = self.conn.execute(
            "SELECT etag, last_modified, status, content_type, body FROM responses WHERE url = ?",
            (url,),
        ).fetchone()
        return CachedFetch(r[0], r[1], int(r[2]), str(r[3]), bytes(r[4])) if r else None

    def put(self, url: str, entry: CachedFetch) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO responses "
                "(url, etag, last_modified, status, content_type, body, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    entry.etag,
                    entry.last_modified,
                    entry.status,
                    entry.content_type,
                    entry.body,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def prune(self, older_than: timedelta) -> int:
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        with transaction(self.conn):
            cur = self.conn.execute("DELETE FROM responses WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount
