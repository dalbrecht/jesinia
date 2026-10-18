# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ConversionStore`` over the module's ``issues.sqlite`` connection: each approved checklist
conversion and the unlinked items it created issues for (``epic_conversions`` and
``converted_items``), so converting the same checklist again never creates an issue twice.
Every query is parameterised."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.issues.application.ports import ConvertedRow
from jesinia.core.db import transaction


class SqliteConversionStore:
    """Checklist conversions in ``issues.sqlite``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        batch_id: str,
        repo: str,
        n: int,
        items: Sequence[tuple[str, str]],
        now: datetime,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT OR IGNORE INTO epic_conversions (batch_id, repo, n, created_at) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, repo, n, now.astimezone(UTC).isoformat(timespec="microseconds")),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO converted_items (repo, n, item_hash, batch_id, temp_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(repo, n, item_hash, batch_id, temp_id) for item_hash, temp_id in items],
            )

    def converted(self, repo: str, n: int) -> list[ConvertedRow]:
        rows = self._conn.execute(
            "SELECT item_hash, batch_id, temp_id, created_repo, created_n FROM converted_items "
            "WHERE repo = ? AND n = ? ORDER BY item_hash",
            (repo, n),
        )
        return [
            ConvertedRow(
                str(item_hash),
                str(batch_id),
                str(temp_id),
                cast(str | None, created_repo),
                cast(int | None, created_n),
            )
            for item_hash, batch_id, temp_id, created_repo, created_n in rows
        ]

    def set_created(
        self, repo: str, n: int, item_hash: str, created_repo: str, created_n: int
    ) -> None:
        self._conn.execute(
            "UPDATE converted_items SET created_repo = ?, created_n = ? "
            "WHERE repo = ? AND n = ? AND item_hash = ?",
            (created_repo, created_n, repo, n, item_hash),
        )

    def running_conversion(self, repo: str, n: int) -> str | None:
        row = self._conn.execute(
            "SELECT epic_conversions.batch_id FROM epic_conversions "
            "JOIN apply_batches ON apply_batches.id = epic_conversions.batch_id "
            "WHERE epic_conversions.repo = ? AND epic_conversions.n = ? "
            "AND apply_batches.status = 'running' LIMIT 1",
            (repo, n),
        ).fetchone()
        return None if row is None else str(row[0])
