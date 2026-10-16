# SPDX-License-Identifier: AGPL-3.0-or-later
"""``FeedStore`` over the module's own ``rss.sqlite`` connection (autocommit, from ``open_db``)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from jesinia.apps.rss.application.ports import NewFeed
from jesinia.apps.rss.domain.feed import Feed, FeedView, Folder, ParsedItem, feed_warning

_FEED_COLUMNS = (
    "id, url, title, site_url, folder_id, interval_s, next_poll_at, fail_count, last_error, "
    "last_ok_at"
)


# The row shape ``_FEED_COLUMNS`` selects.
_FeedRow = tuple[int, str, str, str | None, int | None, int, str, int, str | None, str | None]


def _feed(row: _FeedRow) -> Feed:
    (id_, url, title, site_url, folder_id, interval_s, next_poll_at, fails, error, ok_at) = row
    return Feed(
        id=id_,
        url=url,
        title=title,
        site_url=site_url,
        folder_id=folder_id,
        interval_s=interval_s,
        next_poll_at=datetime.fromisoformat(next_poll_at),
        fail_count=fails,
        last_error=error,
        last_ok_at=datetime.fromisoformat(ok_at) if ok_at is not None else None,
    )


class SqliteFeedStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _one_feed(self, where: str, value: object) -> Feed | None:
        row = self._conn.execute(
            f"SELECT {_FEED_COLUMNS} FROM feeds WHERE {where} = ?", (value,)
        ).fetchone()
        return _feed(row) if row is not None else None

    def feed(self, feed_id: int) -> Feed | None:
        return self._one_feed("id", feed_id)

    def feed_by_url(self, url: str) -> Feed | None:
        return self._one_feed("url", url)

    def insert_feed(self, feed: NewFeed) -> Feed:
        cursor = self._conn.execute(
            "INSERT INTO feeds (url, title, site_url, folder_id, interval_s, next_poll_at, "
            "last_ok_at, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                feed.url,
                feed.title,
                feed.site_url,
                feed.folder_id,
                feed.interval_s,
                feed.next_poll_at.isoformat(),
                feed.last_ok_at.isoformat() if feed.last_ok_at is not None else None,
                feed.last_error,
            ),
        )
        inserted = self.feed(int(cursor.lastrowid or 0))
        assert inserted is not None  # just inserted on this connection
        return inserted

    def delete_feed(self, feed_id: int) -> bool:
        # Posts go with the feed (ON DELETE CASCADE; open_db turns foreign keys on).
        return self._conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,)).rowcount == 1

    def set_feed_folder(self, feed_id: int, folder_id: int | None) -> bool:
        moved = self._conn.execute(
            "UPDATE feeds SET folder_id = ? WHERE id = ?", (folder_id, feed_id)
        )
        return moved.rowcount == 1

    def add_posts(self, feed_id: int, items: Sequence[ParsedItem], now: datetime) -> int:
        rows = [
            (
                feed_id,
                item.guid,
                item.url,
                item.title,
                item.author,
                item.published_at.isoformat() if item.published_at is not None else None,
                item.snippet,
                item.content_html,
                item.discussion_url,
                int(item.is_summary),
                now.isoformat(),
            )
            for item in items
        ]
        with self._transaction():
            cursor = self._conn.executemany(
                "INSERT INTO posts (feed_id, guid, url, title, author, published_at, snippet, "
                "content_html, discussion_url, is_summary, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (feed_id, guid) DO NOTHING",
                rows,
            )
        return max(cursor.rowcount, 0)

    def _one_folder(self, where: str, value: object) -> Folder | None:
        row = self._conn.execute(
            f"SELECT id, name, position FROM folders WHERE {where} = ?", (value,)
        ).fetchone()
        return Folder(int(row[0]), str(row[1]), int(row[2])) if row is not None else None

    def folder(self, folder_id: int) -> Folder | None:
        return self._one_folder("id", folder_id)

    def folder_by_name(self, name: str) -> Folder | None:
        return self._one_folder("name", name)  # the column is COLLATE NOCASE

    def ensure_folder(self, name: str) -> Folder:
        self._conn.execute(
            "INSERT INTO folders (name, position) "
            "VALUES (?, (SELECT COALESCE(MAX(position), -1) + 1 FROM folders)) "
            "ON CONFLICT (name) DO NOTHING",
            (name,),
        )
        found = self.folder_by_name(name)
        assert found is not None  # inserted above, or already there
        return found

    def rename_folder(self, folder_id: int, name: str) -> Folder:
        self._conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
        renamed = self.folder(folder_id)
        assert renamed is not None  # the service checked it exists
        return renamed

    def drop_empty_folders(self) -> int:
        dropped = self._conn.execute(
            "DELETE FROM folders WHERE id NOT IN "
            "(SELECT folder_id FROM feeds WHERE folder_id IS NOT NULL)"
        )
        return dropped.rowcount

    def feed_views(self) -> list[FeedView]:
        rows = self._conn.execute(
            "SELECT f.id, f.title, f.url, d.name, f.folder_id, "
            "(SELECT COUNT(*) FROM posts p WHERE p.feed_id = f.id AND p.read = 0), "
            "f.fail_count, f.last_error "
            "FROM feeds f LEFT JOIN folders d ON d.id = f.folder_id "
            "ORDER BY d.position IS NULL, d.position, f.title COLLATE NOCASE, f.id"
        ).fetchall()
        return [
            FeedView(
                id=int(row[0]),
                title=str(row[1]),
                url=str(row[2]),
                folder=None if row[3] is None else str(row[3]),
                folder_id=None if row[4] is None else int(row[4]),
                unread=int(row[5]),
                warning=feed_warning(int(row[6]), row[7]),
            )
            for row in rows
        ]
