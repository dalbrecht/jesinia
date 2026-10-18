# SPDX-License-Identifier: AGPL-3.0-or-later
"""``QueueStore`` over the module's ``issues.sqlite`` connection: the work queues and their
items (``queues``, ``queue_items``; Task 9). Local only, never synced. Every query is
parameterised."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.issues.domain.epics import ItemKind
from jesinia.apps.issues.domain.queues import Queue, QueueItem, parse_ref
from jesinia.core.db import transaction

_QUEUE_COLUMNS = "id, name, description, position, created_at"
_ITEM_COLUMNS = "kind, ref, position, note, added_at, done_at, done_pending"
# SQLite's default limit on bound parameters is far above this; chunks keep queries short.
_CHUNK = 400


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _optional_time(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _queue(row: tuple[object, ...]) -> Queue:
    queue_id, name, description, position, created = row
    return Queue(
        str(queue_id), str(name), str(description), cast(int, position),
        datetime.fromisoformat(str(created)),
    )  # fmt: skip


def _item(row: Sequence[object]) -> QueueItem:
    kind, ref, position, note, added, done, pending = row
    return QueueItem(
        parse_ref(str(kind), str(ref)),
        cast(int, position),
        str(note),
        datetime.fromisoformat(str(added)),
        _optional_time(done),
        cast(str | None, pending),
    )


class SqliteQueueStore:
    """Work queues in ``issues.sqlite``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def queues(self) -> list[Queue]:
        rows = self._conn.execute(f"SELECT {_QUEUE_COLUMNS} FROM queues ORDER BY position, name")
        return [_queue(row) for row in rows]

    def queue(self, queue_id: str) -> Queue | None:
        row = self._conn.execute(
            f"SELECT {_QUEUE_COLUMNS} FROM queues WHERE id = ?", (queue_id,)
        ).fetchone()
        return None if row is None else _queue(row)

    def queue_named(self, name: str) -> Queue | None:
        row = self._conn.execute(
            f"SELECT {_QUEUE_COLUMNS} FROM queues WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else _queue(row)

    def save_queue(self, queue: Queue) -> None:
        self._conn.execute(
            f"INSERT INTO queues ({_QUEUE_COLUMNS}) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, "
            "description = excluded.description, position = excluded.position",
            (queue.id, queue.name, queue.description, queue.position, _time(queue.created_at)),
        )

    def set_queue_order(self, queue_ids: Sequence[str]) -> None:
        with transaction(self._conn):
            self._conn.executemany(
                "UPDATE queues SET position = ? WHERE id = ?",
                [(position, queue_id) for position, queue_id in enumerate(queue_ids)],
            )

    def delete_queue(self, queue_id: str) -> None:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM queue_items WHERE queue_id = ?", (queue_id,))
            self._conn.execute("DELETE FROM queues WHERE id = ?", (queue_id,))

    def items(self, queue_id: str) -> list[QueueItem]:
        rows = self._conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM queue_items WHERE queue_id = ? ORDER BY position",
            (queue_id,),
        )
        return [_item(row) for row in rows]

    def save_items(self, queue_id: str, items: Sequence[QueueItem]) -> None:
        with transaction(self._conn):
            self._replace(queue_id, items)

    def save_many(self, queues: Mapping[str, Sequence[QueueItem]]) -> None:
        with transaction(self._conn):
            for queue_id, items in queues.items():
                self._replace(queue_id, items)

    def items_with_pending(self, pending_id: str) -> list[tuple[str, QueueItem]]:
        rows = self._conn.execute(
            f"SELECT queue_id, {_ITEM_COLUMNS} FROM queue_items WHERE done_pending = ?",
            (pending_id,),
        )
        return [(str(row[0]), _item(row[1:])) for row in rows]

    def _replace(self, queue_id: str, items: Sequence[QueueItem]) -> None:
        self._conn.execute("DELETE FROM queue_items WHERE queue_id = ?", (queue_id,))
        self._conn.executemany(
            f"INSERT INTO queue_items (queue_id, {_ITEM_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    queue_id,
                    item.ref.kind,
                    item.ref.ref,
                    item.position,
                    item.note,
                    _time(item.added_at),
                    None if item.done_at is None else _time(item.done_at),
                    item.done_pending,
                )
                for item in items
            ],
        )

    def open_items_for(
        self, kinds: Sequence[ItemKind], refs: Sequence[str]
    ) -> list[tuple[str, QueueItem]]:
        found: list[tuple[str, QueueItem]] = []
        kind_marks = ", ".join("?" for _ in kinds)
        for start in range(0, len(refs), _CHUNK):
            chunk = refs[start : start + _CHUNK]
            rows = self._conn.execute(
                f"SELECT queue_id, {_ITEM_COLUMNS} FROM queue_items WHERE done_at IS NULL "
                f"AND kind IN ({kind_marks}) AND ref IN ({', '.join('?' for _ in chunk)})",
                [*kinds, *chunk],
            )
            found += [(str(row[0]), _item(row[1:])) for row in rows]
        return found

    def set_done(self, queue_id: str, item: QueueItem, done_at: datetime) -> None:
        self._conn.execute(
            "UPDATE queue_items SET done_at = ?, done_pending = NULL "
            "WHERE queue_id = ? AND kind = ? AND ref = ? AND done_at IS NULL",
            (_time(done_at), queue_id, item.ref.kind, item.ref.ref),
        )
