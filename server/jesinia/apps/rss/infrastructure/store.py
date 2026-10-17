# SPDX-License-Identifier: AGPL-3.0-or-later
"""``FeedStore`` over the module's own ``rss.sqlite`` connection (autocommit, from ``open_db``)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta

from jesinia.apps.rss.application.ports import NewFeed, PollState
from jesinia.apps.rss.domain.feed import Feed, FeedView, Folder, ParsedItem, feed_warning
from jesinia.apps.rss.domain.kept import ArchiveState
from jesinia.apps.rss.domain.post import TOMBSTONE_TTL, Counts, Cursor, PostView, View, utc_text

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


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class SqliteFeedStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn)

    def atomic(self) -> AbstractContextManager[None]:
        return _transaction(self._conn)

    def _one_feed(self, where: str, value: object) -> Feed | None:
        row = self._conn.execute(
            f"SELECT {_FEED_COLUMNS} FROM feeds WHERE {where} = ?", (value,)
        ).fetchone()
        return _feed(row) if row is not None else None

    def feed(self, feed_id: int) -> Feed | None:
        return self._one_feed("id", feed_id)

    def feed_by_url(self, url: str) -> Feed | None:
        return self._one_feed("url", url)

    def feeds(self) -> list[Feed]:
        rows = self._conn.execute(f"SELECT {_FEED_COLUMNS} FROM feeds ORDER BY id").fetchall()
        return [_feed(row) for row in rows]

    def set_poll_state(self, feed_id: int, state: PollState) -> None:
        self._conn.execute(
            "UPDATE feeds SET interval_s = ?, next_poll_at = ?, fail_count = ?, last_error = ?, "
            "last_ok_at = ? WHERE id = ?",
            (
                state.interval_s,
                state.next_poll_at.isoformat(),
                state.fail_count,
                state.last_error,
                state.last_ok_at.isoformat() if state.last_ok_at is not None else None,
                feed_id,
            ),
        )

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
        guids = [(utc_text(now), feed_id, item.guid) for item in items]
        rows = [
            (
                feed_id,
                item.guid,
                item.url,
                item.title,
                item.author,
                utc_text(item.published_at) if item.published_at is not None else None,
                item.snippet,
                item.content_html,
                item.discussion_url,
                int(item.is_summary),
                utc_text(now),
                feed_id,
                item.guid,
            )
            for item in items
        ]
        with self._transaction():
            # The feed was read in full now: tombstones age relative to this (``prune``).
            self._conn.execute(
                "UPDATE feeds SET last_parsed_at = ? WHERE id = ?", (utc_text(now), feed_id)
            )
            # A pruned item the feed still offers stays pruned, and its tombstone stays fresh.
            self._conn.executemany(
                "UPDATE pruned_posts SET seen_at = ? WHERE feed_id = ? AND guid = ?", guids
            )
            cursor = self._conn.executemany(
                "INSERT INTO posts (feed_id, guid, url, title, author, published_at, snippet, "
                "content_html, discussion_url, is_summary, first_seen_at) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM pruned_posts WHERE feed_id = ? AND guid = ?) "
                "ON CONFLICT (feed_id, guid) DO NOTHING",
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


# A post's place in the newest-first order (``PostView.cursor`` in SQL).
_AT = "COALESCE(p.published_at, p.first_seen_at)"
_POST_COLUMNS = (
    "p.id, p.feed_id, f.title, p.url, p.title, p.author, p.published_at, p.first_seen_at, "
    "p.snippet, p.content_html, p.discussion_url, p.is_summary, p.read, p.kept_page_id, "
    "p.archive_state, p.archive_error, p.full_md IS NOT NULL"
)
_FROM = "FROM posts p JOIN feeds f ON f.id = p.feed_id"
# Fixed SQL per view: callers never put text of their own into a query.
_VIEW_WHERE: dict[str, str] = {
    "unread": "p.read = 0",
    "kept": "p.kept_page_id IS NOT NULL",
    "all": "1",
}
# A page's archive state, from the posts kept to it: the most advanced one wins.
_ARCHIVE_PRECEDENCE: tuple[ArchiveState, ...] = ("done", "archiving", "failed")
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


# The row shape ``_POST_COLUMNS`` selects.
_PostRow = tuple[
    int, int, str, str, str, str | None, str | None, str, str, str | None, str | None,
    int, int, str | None, str | None, str | None, int,
]  # fmt: skip


def _post(row: _PostRow) -> PostView:
    (id_, feed_id, feed_title, url, title, author, published, seen, snippet, content,
     discussion, is_summary, read, kept, archive_state, archive_error, has_full) = row  # fmt: skip
    return PostView(
        id=id_,
        feed_id=feed_id,
        feed_title=feed_title,
        url=url,
        title=title,
        author=author,
        published_at=datetime.fromisoformat(published) if published is not None else None,
        first_seen_at=datetime.fromisoformat(seen),
        snippet=snippet,
        content_html=content,
        discussion_url=discussion,
        is_summary=bool(is_summary),
        read=bool(read),
        kept_page_id=kept,
        archive_state=archive_state,
        archive_error=archive_error,
        has_full_article=bool(has_full),
    )


def _scope(
    view: View, feed_id: int | None, folder_id: int | None
) -> tuple[list[str], list[object]]:
    """The WHERE terms (and their parameters) selecting a view within a feed or folder."""
    where = [_VIEW_WHERE[view]]
    params: list[object] = []
    if feed_id is not None:
        where.append("p.feed_id = ?")
        params.append(feed_id)
    if folder_id is not None:
        where.append("f.folder_id = ?")
        params.append(folder_id)
    return where, params


class SqlitePostStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn)

    def posts(
        self,
        view: View,
        feed_id: int | None,
        folder_id: int | None,
        before: Cursor | None,
        limit: int,
    ) -> list[PostView]:
        where, params = _scope(view, feed_id, folder_id)
        if before is not None:
            where.append(f"({_AT} < ? OR ({_AT} = ? AND p.id < ?))")
            params += [before[0], before[0], before[1]]
        rows = self._conn.execute(
            f"SELECT {_POST_COLUMNS} {_FROM} WHERE {' AND '.join(where)} "
            f"ORDER BY {_AT} DESC, p.id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_post(row) for row in rows]

    def post(self, post_id: int) -> PostView | None:
        row = self._conn.execute(
            f"SELECT {_POST_COLUMNS} {_FROM} WHERE p.id = ?", (post_id,)
        ).fetchone()
        return _post(row) if row is not None else None

    def full_md(self, post_id: int) -> str | None:
        row = self._conn.execute("SELECT full_md FROM posts WHERE id = ?", (post_id,)).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def set_full_md(self, post_id: int, markdown: str) -> None:
        self._conn.execute("UPDATE posts SET full_md = ? WHERE id = ?", (markdown, post_id))

    def set_read(self, ids: Sequence[int], read: bool) -> int:
        marks = ",".join("?" * len(ids))
        updated = self._conn.execute(
            f"UPDATE posts SET read = ? WHERE id IN ({marks})", (int(read), *ids)
        )
        return updated.rowcount

    def mark_read_from(
        self,
        view: View,
        feed_id: int | None,
        folder_id: int | None,
        start: Cursor,
        as_of: datetime,
    ) -> int:
        where, params = _scope(view, feed_id, folder_id)
        where.append(f"({_AT} > ? OR ({_AT} = ? AND p.id >= ?))")
        where.append("p.first_seen_at <= ?")
        params += [start[0], start[0], start[1], utc_text(as_of)]
        marked = self._conn.execute(
            f"UPDATE posts SET read = 1 WHERE id IN (SELECT p.id {_FROM} WHERE "
            f"{' AND '.join(where)})",
            params,
        )
        return marked.rowcount

    def search(self, text: str, limit: int) -> list[PostView]:
        pattern = f"%{text.translate(_LIKE_ESCAPE)}%"
        rows = self._conn.execute(
            f"SELECT {_POST_COLUMNS} {_FROM} "
            "WHERE p.title LIKE ? ESCAPE '\\' OR p.snippet LIKE ? ESCAPE '\\' "
            f"ORDER BY {_AT} DESC, p.id DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return [_post(row) for row in rows]

    def prune(self, cutoff: datetime, now: datetime) -> int:
        old = "FROM posts WHERE kept_page_id IS NULL AND COALESCE(published_at, first_seen_at) < ?"
        with self._transaction():
            # Forgotten once the feed was read in full without offering it for the TTL: an
            # item it still lists keeps a fresh tombstone at every full read.
            self._conn.execute(
                "DELETE FROM pruned_posts WHERE julianday(seen_at) < "
                "(SELECT julianday(f.last_parsed_at) - ? FROM feeds f "
                "WHERE f.id = pruned_posts.feed_id)",
                (TOMBSTONE_TTL / timedelta(days=1),),
            )
            self._conn.execute(
                f"INSERT INTO pruned_posts (feed_id, guid, seen_at) SELECT feed_id, guid, ? {old} "
                "ON CONFLICT (feed_id, guid) DO UPDATE SET seen_at = excluded.seen_at",
                (utc_text(now), utc_text(cutoff)),
            )
            deleted = self._conn.execute(f"DELETE {old}", (utc_text(cutoff),))
        return deleted.rowcount

    def counts(self) -> Counts:
        feeds = self._conn.execute(
            "SELECT f.id, (SELECT COUNT(*) FROM posts p WHERE p.feed_id = f.id AND p.read = 0) "
            "FROM feeds f ORDER BY f.id"
        ).fetchall()
        folders = self._conn.execute(
            "SELECT d.id, (SELECT COUNT(*) FROM posts p JOIN feeds f ON f.id = p.feed_id "
            "WHERE f.folder_id = d.id AND p.read = 0) FROM folders d ORDER BY d.position"
        ).fetchall()
        unread, kept, total = self._conn.execute(
            "SELECT COALESCE(SUM(read = 0), 0), COALESCE(SUM(kept_page_id IS NOT NULL), 0), "
            "COUNT(*) FROM posts"
        ).fetchone()
        return Counts(
            feeds={int(r[0]): int(r[1]) for r in feeds},
            folders={int(r[0]): int(r[1]) for r in folders},
            unread=int(unread),
            kept=int(kept),
            all=int(total),
        )

    def set_kept(self, post_id: int, page_id: str, state: ArchiveState) -> None:
        # One statement: the page and the archive state are recorded together.
        self._conn.execute(
            "UPDATE posts SET kept_page_id = ?, archive_state = ?, archive_error = NULL "
            "WHERE id = ?",
            (page_id, state, post_id),
        )

    def set_post_archive_state(
        self, post_id: int, state: ArchiveState | None, error: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE posts SET archive_state = ?, archive_error = ? WHERE id = ?",
            (state, error, post_id),
        )

    def page_archive_state(self, page_id: str) -> ArchiveState | None:
        rows = self._conn.execute(
            "SELECT DISTINCT archive_state FROM posts WHERE kept_page_id = ?", (page_id,)
        ).fetchall()
        states = {row[0] for row in rows}
        return next((state for state in _ARCHIVE_PRECEDENCE if state in states), None)

    def set_archive_state(self, page_id: str, state: ArchiveState, error: str | None) -> None:
        self._conn.execute(
            "UPDATE posts SET archive_state = ?, archive_error = ? WHERE kept_page_id = ?",
            (state, error, page_id),
        )

    def archiving(self) -> list[tuple[str | None, int]]:
        rows = self._conn.execute(
            "SELECT kept_page_id, MIN(id) FROM posts WHERE archive_state = 'archiving' "
            "GROUP BY COALESCE(kept_page_id, 'post:' || id) ORDER BY MIN(id)"
        ).fetchall()
        return [(None if row[0] is None else str(row[0]), int(row[1])) for row in rows]
