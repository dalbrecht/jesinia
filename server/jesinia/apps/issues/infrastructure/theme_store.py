# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ThemeStore`` over the module's ``issues.sqlite`` connection: each issue's themes
(``issue_themes``) and the batches that turned a theme into an epic (``theme_epics``). Every
query is parameterised."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from jesinia.core.db import transaction


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class SqliteThemeStore:
    """Themes in ``issues.sqlite``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(
        self, refs: Sequence[tuple[str, int]], tag: str, now: datetime
    ) -> list[tuple[str, int]]:
        added: list[tuple[str, int]] = []
        with transaction(self._conn):
            for repo, n in refs:
                inserted = self._conn.execute(
                    "INSERT OR IGNORE INTO issue_themes (repo, n, tag, added_at) "
                    "VALUES (?, ?, ?, ?)",
                    (repo, n, tag, _time(now)),
                ).rowcount
                if inserted:
                    added.append((repo, n))
        return added

    def remove(self, refs: Sequence[tuple[str, int]], tag: str) -> int:
        with transaction(self._conn):
            return sum(
                self._conn.execute(
                    "DELETE FROM issue_themes WHERE repo = ? AND n = ? AND tag = ?",
                    (repo, n, tag),
                ).rowcount
                for repo, n in refs
            )

    def themes(self) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT tag, COUNT(*) FROM issue_themes GROUP BY tag ORDER BY tag"
        )
        return [(str(tag), int(count)) for tag, count in rows]

    def issues(self, tag: str) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT repo, n FROM issue_themes WHERE tag = ? ORDER BY repo COLLATE NOCASE, n",
            (tag,),
        )
        return [(str(repo), int(n)) for repo, n in rows]

    def of_issue(self, repo: str, n: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT tag FROM issue_themes WHERE repo = ? AND n = ? ORDER BY tag", (repo, n)
        )
        return [str(tag) for (tag,) in rows]

    def record_epic(self, tag: str, batch_id: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO theme_epics (tag, batch_id, created_at) VALUES (?, ?, ?)",
            (tag, batch_id, _time(now)),
        )

    def epic_batches(self, tag: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT batch_id FROM theme_epics WHERE tag = ? ORDER BY created_at, rowid", (tag,)
        )
        return [str(batch_id) for (batch_id,) in rows]
