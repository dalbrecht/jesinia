# SPDX-License-Identifier: AGPL-3.0-or-later
"""``notes.sqlite`` (derived class)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from jesinia.core.db import transaction


class SqliteNoteLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, note_id: str, session_id: str, subject_id: str, at: datetime) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO saved_notes (note_id, session_id, subject_id, saved_at) "
                "VALUES (?, ?, ?, ?)",
                (note_id, session_id, subject_id, at.isoformat()),
            )

    def count_for_session(self, session_id: str) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM saved_notes WHERE session_id = ?", (session_id,)
        )
        return int(r.fetchone()[0])

    def notes_for(self, subject_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT note_id FROM saved_notes WHERE subject_id = ? ORDER BY saved_at", (subject_id,)
        )
        return [str(r[0]) for r in rows]
