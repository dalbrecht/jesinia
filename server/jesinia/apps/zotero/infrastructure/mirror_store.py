# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite mirror of the Zotero library (``zotero_mirror.sqlite``, derived)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, cast

from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.domain.paper import (
    Annotation,
    Attachment,
    Creator,
    Note,
    Paper,
    ZoteroTag,
)

_LIBRARY_VERSION = "library_version"
_SERVER_ID = "server_id"


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _to_paper(data: dict[str, Any]) -> Paper:
    return Paper(
        **{
            **data,
            "creators": tuple(Creator(**c) for c in data["creators"]),
            "tags": tuple(ZoteroTag(**t) for t in data["tags"]),
            "related": tuple(data["related"]),
            "notes": tuple(Note(**n) for n in data["notes"]),
            "attachments": tuple(
                Attachment(
                    **{
                        **a,
                        "annotations": tuple(Annotation(**x) for x in a["annotations"]),
                    }
                )
                for a in data["attachments"]
            ),
        }
    )


class SqliteMirrorStore:
    """Implements :class:`MirrorStore` over an autocommit connection from ``open_db``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: ItemKey) -> Paper | None:
        row = self._conn.execute("SELECT json FROM papers WHERE key = ?", (key,)).fetchone()
        return None if row is None else _to_paper(json.loads(row[0]))

    def put(
        self,
        paper: Paper,
        fulltext: str | None,
        fulltext_versions: Mapping[ItemKey, int] | None = None,
    ) -> None:
        versions = fulltext_versions or {}
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO papers (key, version, json) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET version = excluded.version, json = excluded.json",
                (paper.key, paper.version, json.dumps(asdict(paper))),
            )
            self._conn.execute("DELETE FROM attachments WHERE paper_key = ?", (paper.key,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO attachments (attachment_key, paper_key, fulltext_version) "
                "VALUES (?, ?, ?)",
                [(a.key, paper.key, versions.get(a.key, 0)) for a in paper.attachments],
            )
            if fulltext is None:
                self._conn.execute("DELETE FROM fulltext WHERE key = ?", (paper.key,))
            else:
                self._conn.execute(
                    "INSERT INTO fulltext (key, text) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET text = excluded.text",
                    (paper.key, fulltext),
                )

    def remove(self, key: ItemKey) -> None:
        with _transaction(self._conn):
            self._conn.execute("DELETE FROM papers WHERE key = ?", (key,))
            self._conn.execute("DELETE FROM fulltext WHERE key = ?", (key,))
            self._conn.execute("DELETE FROM attachments WHERE paper_key = ?", (key,))

    def clear(self) -> None:
        with _transaction(self._conn):
            self._conn.execute("DELETE FROM papers")
            self._conn.execute("DELETE FROM fulltext")
            self._conn.execute("DELETE FROM attachments")
            self._conn.execute("DELETE FROM meta WHERE name = ?", (_LIBRARY_VERSION,))

    def all_keys(self) -> list[ItemKey]:
        rows = self._conn.execute("SELECT key FROM papers ORDER BY key").fetchall()
        return [ItemKey(row[0]) for row in rows]

    def owner_of(self, attachment_key: ItemKey) -> ItemKey | None:
        row = self._conn.execute(
            "SELECT paper_key FROM attachments WHERE attachment_key = ?", (attachment_key,)
        ).fetchone()
        return None if row is None else ItemKey(row[0])

    def _meta(self, name: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return None if row is None else cast(str, row[0])

    def _set_meta(self, name: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (name, value) VALUES (?, ?) "
            "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
            (name, value),
        )

    def library_version(self) -> int:
        value = self._meta(_LIBRARY_VERSION)
        return 0 if value is None else int(value)

    def set_library_version(self, version: int) -> None:
        self._set_meta(_LIBRARY_VERSION, str(version))

    def server_id(self) -> str | None:
        return self._meta(_SERVER_ID)

    def set_server_id(self, server_id: str) -> None:
        self._set_meta(_SERVER_ID, server_id)

    def fulltext(self, key: ItemKey) -> str | None:
        row = self._conn.execute("SELECT text FROM fulltext WHERE key = ?", (key,)).fetchone()
        return None if row is None else cast(str, row[0])

    def fulltext_version(self, attachment_key: ItemKey) -> int:
        row = self._conn.execute(
            "SELECT fulltext_version FROM attachments WHERE attachment_key = ?", (attachment_key,)
        ).fetchone()
        return 0 if row is None else int(row[0])
