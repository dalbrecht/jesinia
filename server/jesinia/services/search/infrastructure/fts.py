# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite FTS5 implementation of the search index (``search.sqlite``, derived)."""

from __future__ import annotations

import html
import sqlite3
from collections.abc import Sequence

from jesinia.core.db import transaction
from jesinia.services.search.application.service import IndexedDoc, SearchHit

_OPEN, _CLOSE = "", ""  # sentinels; the snippet is escaped, then marked up


def _snippet_html(raw: str) -> str:
    return html.escape(raw).replace(_OPEN, "<mark>").replace(_CLOSE, "</mark>")


class FtsIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, doc: IndexedDoc) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM docs WHERE source = ? AND key = ?", (doc.source, doc.key)
            )
            self.conn.execute(
                "INSERT INTO docs (source, key, title, body, url, path, meta, authoritative) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.source,
                    doc.key,
                    doc.title,
                    doc.body,
                    doc.url,
                    doc.path,
                    doc.meta,
                    int(doc.authoritative),
                ),
            )

    def delete(self, source: str, key: str) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM docs WHERE source = ? AND key = ?", (source, key))

    def clear_source(self, source: str) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM docs WHERE source = ?", (source,))

    def match(self, fts_query: str, limit: int, sources: Sequence[str] | None) -> list[SearchHit]:
        sql = (
            "SELECT source, key, title, path, url, authoritative, "
            # -1: the snippet comes from whichever column matched (body, path or metadata).
            f"snippet(docs, -1, '{_OPEN}', '{_CLOSE}', '…', 16), "
            "bm25(docs, 0, 0, 8.0, 1.0, 0, 2.0, 1.0, 0) AS score "
            "FROM docs WHERE docs MATCH ?"
        )
        params: list[object] = [fts_query]
        if sources:
            sql += f" AND source IN ({', '.join('?' for _ in sources)})"
            params += list(sources)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            SearchHit(
                source=r[0],
                key=r[1],
                title=r[2],
                snippet=_snippet_html(r[6] or ""),
                path=r[3],
                authoritative=bool(int(r[5])),
                score=float(r[7]),
                url=r[4],
            )
            for r in rows
        ]
