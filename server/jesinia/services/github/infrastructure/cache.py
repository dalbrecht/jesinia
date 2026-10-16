# SPDX-License-Identifier: AGPL-3.0-or-later
"""``github.sqlite`` (cache class): ETag-keyed REST responses."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from jesinia.core.db import transaction
from jesinia.services.github.application.ports import CachedResponse


class SqliteGitHubCache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> CachedResponse | None:
        r = self.conn.execute(
            "SELECT etag, status, data FROM responses WHERE key = ?", (key,)
        ).fetchone()
        return CachedResponse(r[0], int(r[1]), json.loads(r[2])) if r else None

    def put(self, key: str, etag: str, status: int, data: object) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO responses (key, etag, status, data, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, etag, status, json.dumps(data), datetime.now(UTC).isoformat()),
            )

    def prune(self, older_than: timedelta) -> int:
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        with transaction(self.conn):
            cur = self.conn.execute("DELETE FROM responses WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount
