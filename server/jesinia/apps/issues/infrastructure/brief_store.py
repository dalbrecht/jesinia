# SPDX-License-Identifier: AGPL-3.0-or-later
"""``BriefStore`` over the module's ``issues.sqlite`` connection: each epic's delivery brief
comment (``epic_briefs``, ruling IR8). Every query is parameterised."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.issues.application.ports import BriefState, StoredBrief

_COLUMNS = "repo, n, comment_id, url, body, state, attempt_at, posted_at, updated_at"
_STATES: tuple[BriefState, ...] = ("in_flight", "posted")


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _optional_time(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _brief(row: tuple[object, ...]) -> StoredBrief:
    repo, n, comment_id, url, body, state, attempt, posted, updated = row
    return StoredBrief(
        repo=str(repo),
        n=cast(int, n),
        comment_id=cast(int | None, comment_id),
        url=cast(str | None, url),
        body=str(body),
        state=next(s for s in _STATES if s == state),
        attempt_at=_optional_time(attempt),
        posted_at=_optional_time(posted),
        updated_at=datetime.fromisoformat(str(updated)),
    )


class SqliteBriefStore:
    """Delivery brief comments in ``issues.sqlite``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def brief(self, repo: str, n: int) -> StoredBrief | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM epic_briefs WHERE repo = ? AND n = ?", (repo, n)
        ).fetchone()
        return None if row is None else _brief(row)

    def mark_in_flight(self, repo: str, n: int, body: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO epic_briefs (repo, n, comment_id, url, body, state, attempt_at, "
            "updated_at) VALUES (?, ?, NULL, NULL, ?, 'in_flight', ?, ?) "
            "ON CONFLICT (repo, n) DO UPDATE SET comment_id = NULL, url = NULL, "
            "body = excluded.body, state = 'in_flight', attempt_at = excluded.attempt_at, "
            "updated_at = excluded.updated_at",
            (repo, n, body, _time(now), _time(now)),
        )

    def mark_posted(
        self, repo: str, n: int, comment_id: int, url: str, body: str, now: datetime
    ) -> StoredBrief:
        self._conn.execute(
            "INSERT INTO epic_briefs (repo, n, comment_id, url, body, state, posted_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, 'posted', ?, ?) "
            "ON CONFLICT (repo, n) DO UPDATE SET comment_id = excluded.comment_id, "
            "url = excluded.url, body = excluded.body, state = 'posted', attempt_at = NULL, "
            "posted_at = excluded.posted_at, updated_at = excluded.updated_at",
            (repo, n, comment_id, url, body, _time(now), _time(now)),
        )
        return StoredBrief(repo, n, comment_id, url, body, "posted", None, now, now)
