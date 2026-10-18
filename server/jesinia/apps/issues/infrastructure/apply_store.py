# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ApplyStore`` over the module's ``issues.sqlite`` connection: the pending-changes tray and
the applied batches with their results. Every query is parameterised."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.issues.application.ports import AppliedBatch, ApplyOrigin
from jesinia.apps.issues.domain.changes import (
    Existing,
    PendingChange,
    batch_json,
    change_json,
    parse_batch,
    parse_batch_status,
    parse_issue_change,
    parse_result,
    parse_source,
    result_json,
)
from jesinia.core.db import transaction

_PENDING_COLUMNS = "id, ref, change_json, created_at, source"
_BATCH_COLUMNS = (
    "id, origin, suggestion_id, batch_json, result_json, created_at, updated_at, status, "
    "request_key"
)
_ORIGINS: tuple[ApplyOrigin, ...] = ("pending", "suggestion")


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _ref(target: Existing) -> str:
    return json.dumps({"repo": target.repo, "n": target.n})


def _pending(row: tuple[object, ...]) -> PendingChange:
    pid, ref, change, created, source = row
    target = json.loads(str(ref))
    return PendingChange(
        id=str(pid),
        target=Existing(str(target["repo"]), int(target["n"])),
        change=parse_issue_change(json.loads(str(change))),
        created_at=datetime.fromisoformat(str(created)),
        source=parse_source(str(source)),
    )


def _batch(row: tuple[object, ...]) -> AppliedBatch:
    bid, origin, suggestion_id, batch_text, result_text, created, updated, status, key = row
    batch = parse_batch(json.loads(str(batch_text)))
    return AppliedBatch(
        id=str(bid),
        origin=next(o for o in _ORIGINS if o == origin),
        suggestion_id=cast(str | None, suggestion_id),
        batch=batch,
        result=parse_result(batch, json.loads(str(result_text))),
        created_at=datetime.fromisoformat(str(created)),
        updated_at=datetime.fromisoformat(str(updated)),
        status=parse_batch_status(status),
        request_key=cast(str | None, key),
    )


class SqliteApplyStore:
    """The pending-changes tray and the applied batches in ``issues.sqlite``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_pending(self, change: PendingChange) -> None:
        self._conn.execute(
            f"INSERT INTO pending ({_PENDING_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (
                change.id,
                _ref(change.target),
                json.dumps(change_json(change.change), ensure_ascii=False),
                _time(change.created_at),
                change.source,
            ),
        )

    def pending(self, ids: Sequence[str] | None = None) -> list[PendingChange]:
        query = f"SELECT {_PENDING_COLUMNS} FROM pending"
        params: list[object] = []
        if ids is not None:
            query += f" WHERE id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)
        rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params)
        return [_pending(row) for row in rows]

    def delete_pending(self, ids: Sequence[str]) -> int:
        return self._delete(ids)

    def _delete(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        marks = ", ".join("?" for _ in ids)
        return self._conn.execute(f"DELETE FROM pending WHERE id IN ({marks})", list(ids)).rowcount

    def start_batch(self, applied: AppliedBatch, taken: Sequence[str]) -> None:
        with transaction(self._conn):
            self.save_batch(applied)
            self._delete(taken)

    def clear_pending(self) -> None:
        self._conn.execute("DELETE FROM pending")

    def save_batch(self, applied: AppliedBatch) -> None:
        self._conn.execute(
            f"INSERT INTO apply_batches ({_BATCH_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET result_json = excluded.result_json, "
            "updated_at = excluded.updated_at, status = excluded.status",
            (
                applied.id,
                applied.origin,
                applied.suggestion_id,
                json.dumps(batch_json(applied.batch), ensure_ascii=False),
                json.dumps(result_json(applied.result), ensure_ascii=False),
                _time(applied.created_at),
                _time(applied.updated_at),
                applied.status,
                applied.request_key,
            ),
        )

    def batch(self, batch_id: str) -> AppliedBatch | None:
        row = self._conn.execute(
            f"SELECT {_BATCH_COLUMNS} FROM apply_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        return None if row is None else _batch(row)

    def batch_by_key(self, request_key: str) -> AppliedBatch | None:
        row = self._conn.execute(
            f"SELECT {_BATCH_COLUMNS} FROM apply_batches WHERE request_key = ?", (request_key,)
        ).fetchone()
        return None if row is None else _batch(row)

    def interrupt_running(self) -> int:
        return self._conn.execute(
            "UPDATE apply_batches SET status = 'interrupted' WHERE status = 'running'"
        ).rowcount

    def prune_batches(self, before: datetime) -> int:
        return self._conn.execute(
            "DELETE FROM apply_batches WHERE updated_at < ? AND status != 'running'",
            (_time(before),),
        ).rowcount
