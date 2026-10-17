# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SuggestionStore`` over the module's ``issues.sqlite`` connection: suggestion drafts, each
issue's latest suggestion run, and the corpus the similarity search reads. Every query is
parameterised; issue text is returned verbatim, as data."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.issues.application.ports import (
    CorpusEntry,
    RunSource,
    RunStatus,
    SuggestionRun,
)
from jesinia.apps.issues.domain.similarity import BODY_CHARS
from jesinia.apps.issues.domain.suggestions import (
    ISSUE_KINDS,
    Suggestion,
    SuggestionFilters,
    SuggestionKind,
    parse_kind,
    parse_state,
    payload_json,
)
from jesinia.core.db import transaction

_COLUMNS = (
    "id, repo, n, scope, kind, payload_json, reason, state, created_at, updated_at, "
    "origin_json, basis, apply_error"
)
_RUN_COLUMNS = "repo, n, status, source, due_at, cause, updated_at"
_STATUSES: tuple[RunStatus, ...] = ("queued", "running", "done", "skipped", "waiting", "failed")
_SOURCES: tuple[RunSource, ...] = ("inbox", "demand")


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _suggestion(row: tuple[object, ...]) -> Suggestion:
    sid, repo, n, scope, kind, payload, reason, state, created, updated, origin, basis, error = row
    return Suggestion(
        id=str(sid),
        repo=cast(str | None, repo),
        n=cast(int | None, n),
        scope=cast(str | None, scope),
        kind=parse_kind(str(kind)),
        payload=json.loads(str(payload)),
        reason=str(reason),
        state=parse_state(str(state)),
        created_at=datetime.fromisoformat(str(created)),
        updated_at=datetime.fromisoformat(str(updated)),
        origin=None if origin is None else tuple(json.loads(str(origin))),
        basis=cast(str | None, basis),
        apply_error=cast(str | None, error),
    )


def _origin_json(origin: tuple[object, ...] | None) -> str | None:
    return None if origin is None else json.dumps(list(origin), ensure_ascii=False)


def _run(row: tuple[object, ...]) -> SuggestionRun:
    repo, n, status, source, due_at, cause, updated = row
    return SuggestionRun(
        repo=str(repo),
        n=cast(int, n),
        status=next(s for s in _STATUSES if s == status),
        source=next(s for s in _SOURCES if s == source),
        due_at=datetime.fromisoformat(str(due_at)),
        cause=cast(str | None, cause),
        updated_at=datetime.fromisoformat(str(updated)),
    )


class SqliteSuggestionStore:
    """Suggestions and their runs in ``issues.sqlite``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # Suggestions ------------------------------------------------------------------------

    def suggestions(self, filters: SuggestionFilters, limit: int | None = None) -> list[Suggestion]:
        where: list[str] = []
        params: list[object] = []
        if filters.repo is not None:
            where.append("repo = ?")
            params.append(filters.repo)
        if filters.n is not None:
            where.append("n = ?")
            params.append(filters.n)
        if filters.states:
            where.append(f"state IN ({', '.join('?' for _ in filters.states)})")
            params.extend(filters.states)
        if filters.kind is not None:
            where.append("kind = ?")
            params.append(filters.kind)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        query = f"SELECT {_COLUMNS}, rowid FROM suggestions {clause}"
        if limit is not None:
            # The newest ``limit``, still listed oldest first.
            query = f"SELECT * FROM ({query}ORDER BY created_at DESC, rowid DESC LIMIT ?) "
            params.append(limit)
        rows = self._conn.execute(f"{query}ORDER BY created_at, rowid", params)
        return [_suggestion(row[:-1]) for row in rows]

    def suggestion(self, suggestion_id: str) -> Suggestion | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        return None if row is None else _suggestion(row)

    def replace_drafts(self, repo: str, n: int, drafts: Sequence[Suggestion]) -> None:
        kinds = ", ".join("?" for _ in ISSUE_KINDS)
        with transaction(self._conn):
            self._conn.execute(
                f"DELETE FROM suggestions WHERE repo = ? AND n = ? AND state = 'draft' "
                f"AND claimed_at IS NULL AND kind IN ({kinds})",
                (repo, n, *ISSUE_KINDS),
            )
            self._insert(drafts)

    def replace_scope_drafts(
        self, scope: str, kind: SuggestionKind, drafts: Sequence[Suggestion]
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "DELETE FROM suggestions WHERE scope = ? AND kind = ? AND repo IS NULL "
                "AND state = 'draft' AND claimed_at IS NULL",
                (scope, kind),
            )
            self._insert(drafts)

    def _insert(self, drafts: Sequence[Suggestion]) -> None:
        self._conn.executemany(
            f"INSERT INTO suggestions ({_COLUMNS}) VALUES ({', '.join('?' for _ in range(13))})",
            [
                (
                    s.id,
                    s.repo,
                    s.n,
                    s.scope,
                    s.kind,
                    payload_json(s.payload),
                    s.reason,
                    s.state,
                    _time(s.created_at),
                    _time(s.updated_at),
                    _origin_json(s.origin),
                    s.basis,
                    s.apply_error,
                )
                for s in drafts
            ],
        )

    def save(self, suggestion: Suggestion) -> None:
        self._conn.execute(
            "UPDATE suggestions SET kind = ?, payload_json = ?, reason = ?, state = ?, "
            "updated_at = ?, origin_json = ?, basis = ?, apply_error = ? WHERE id = ?",
            (
                suggestion.kind,
                payload_json(suggestion.payload),
                suggestion.reason,
                suggestion.state,
                _time(suggestion.updated_at),
                _origin_json(suggestion.origin),
                suggestion.basis,
                suggestion.apply_error,
                suggestion.id,
            ),
        )

    def claim(self, suggestion_id: str, now: datetime) -> bool:
        """Claim an open, unclaimed suggestion for an approval; whether it was claimed."""
        return bool(
            self._conn.execute(
                "UPDATE suggestions SET claimed_at = ? WHERE id = ? "
                "AND state IN ('draft', 'edited') AND claimed_at IS NULL",
                (_time(now), suggestion_id),
            ).rowcount
        )

    def release(self, suggestion_id: str) -> None:
        self._conn.execute(
            "UPDATE suggestions SET claimed_at = NULL WHERE id = ?", (suggestion_id,)
        )

    def release_all(self) -> int:
        """Release every claim (at start nothing is applying: a claim is left by a stop)."""
        return self._conn.execute(
            "UPDATE suggestions SET claimed_at = NULL WHERE claimed_at IS NOT NULL"
        ).rowcount

    def retire(self, suggestion_ids: Sequence[str]) -> None:
        self._conn.executemany(
            "DELETE FROM suggestions WHERE id = ? AND state = 'draft' AND claimed_at IS NULL",
            [(sid,) for sid in suggestion_ids],
        )

    # Runs -------------------------------------------------------------------------------

    def queue_run(
        self, repo: str, n: int, due_at: datetime, source: RunSource, now: datetime
    ) -> None:
        self._conn.execute(
            f"INSERT INTO suggestion_runs ({_RUN_COLUMNS}) VALUES (?, ?, 'queued', ?, ?, NULL, ?) "
            "ON CONFLICT (repo, n) DO UPDATE SET status = 'queued', source = excluded.source, "
            "due_at = excluded.due_at, cause = NULL, updated_at = excluded.updated_at",
            (repo, n, source, _time(due_at), _time(now)),
        )

    def due_runs(self, now: datetime, limit: int) -> list[SuggestionRun]:
        rows = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM suggestion_runs "
            "WHERE status IN ('queued', 'waiting') AND due_at <= ? "
            "ORDER BY due_at, repo, n LIMIT ?",
            (_time(now), limit),
        )
        return [_run(row) for row in rows]

    def set_run(
        self,
        repo: str,
        n: int,
        status: RunStatus,
        cause: str | None,
        now: datetime,
        *,
        only_if: RunStatus | None = None,
    ) -> None:
        guard = "" if only_if is None else " AND status = ?"
        params: tuple[object, ...] = (status, cause, _time(now), repo, n)
        self._conn.execute(
            "UPDATE suggestion_runs SET status = ?, cause = ?, updated_at = ? "
            f"WHERE repo = ? AND n = ?{guard}",
            params if only_if is None else (*params, only_if),
        )

    def run(self, repo: str, n: int) -> SuggestionRun | None:
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM suggestion_runs WHERE repo = ? AND n = ?", (repo, n)
        ).fetchone()
        return None if row is None else _run(row)

    def requeue_running(self, now: datetime) -> int:
        cursor = self._conn.execute(
            "UPDATE suggestion_runs SET status = 'queued', updated_at = ? WHERE status = 'running'",
            (_time(now),),
        )
        return cursor.rowcount

    # The corpus -------------------------------------------------------------------------

    def corpus(self) -> list[CorpusEntry]:
        rows = self._conn.execute(
            "SELECT issues.repo, issues.number, json_extract(issues.json, '$.title'), "
            "substr(json_extract(issues.json, '$.body'), 1, ?), issues.state, issues.is_epic "
            "FROM issues JOIN repos ON repos.repo = issues.repo WHERE repos.watched = 1 "
            "ORDER BY issues.updated_at DESC, issues.repo, issues.number",
            (BODY_CHARS,),
        )
        return [
            CorpusEntry(str(repo), int(n), str(title), str(body), state == "open", bool(epic))
            for repo, n, title, body, state, epic in rows
        ]
