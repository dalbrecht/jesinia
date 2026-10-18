# SPDX-License-Identifier: AGPL-3.0-or-later
"""``KnowledgeStore`` over the module's ``review.sqlite`` connection (migration 0009).

Times are stored as ISO 8601 UTC text with microseconds, so they compare as text.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.review.application.knowledge import (
    ChurnRow,
    ExposureRow,
    PendingRound,
    RepoState,
)
from jesinia.apps.review.domain.knowledge import (
    Exposure,
    ExposureKind,
    InvalidUnitError,
    Override,
    Unit,
    parse_unit,
    quiz_kind,
)
from jesinia.apps.review.domain.queue import PrRef
from jesinia.core.db import transaction

log = logging.getLogger(__name__)


def _time(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _unit(text: str) -> Unit | None:
    try:
        return parse_unit(text)
    except InvalidUnitError:  # written by this store, so never expected; skipped, not fatal
        log.warning("skipping a stored knowledge unit that no longer parses: %r", text)
        return None


def _paths(stored: str | None, cached: str | None) -> tuple[str, ...] | None:
    """A round's touched paths: stored with it, else its PR's cached files, else unknown."""
    if stored is not None:
        return tuple(str(path) for path in json.loads(stored))
    if cached is None:
        return None
    return tuple(str(item["filename"]) for item in json.loads(cached))


class SqliteKnowledgeStore:
    """The knowledge model's records; shares the review module's connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- exposures and overrides ---------------------------------------------------------

    def exposures(self) -> dict[Unit, list[Exposure]]:
        found: dict[Unit, list[Exposure]] = {}
        parsed: dict[str, Unit | None] = {}
        for text, kind, at in self._conn.execute("SELECT unit, kind, at FROM knowledge_exposures"):
            unit = parsed[text] if text in parsed else parsed.setdefault(text, _unit(text))
            if unit is not None:
                found.setdefault(unit, []).append(Exposure(cast(ExposureKind, kind), _parse(at)))
        return found

    def overrides(self) -> dict[Unit, Override]:
        rows = self._conn.execute("SELECT unit, override FROM knowledge_overrides")
        return {
            unit: cast(Override, override)
            for text, override in rows
            if (unit := _unit(text)) is not None
        }

    def set_override(self, unit: Unit, override: Override | None, at: datetime) -> None:
        if override is None:
            self._conn.execute("DELETE FROM knowledge_overrides WHERE unit = ?", (unit.id,))
            return
        self._conn.execute(
            "INSERT INTO knowledge_overrides (unit, override, set_at) VALUES (?, ?, ?) "
            "ON CONFLICT (unit) DO UPDATE SET override = excluded.override, "
            "set_at = excluded.set_at",
            (unit.id, override, _time(at)),
        )

    def changes_since(self, unit: Unit, since: datetime) -> int:
        if unit.repo is None:
            return 0
        row = self._conn.execute(
            "SELECT COALESCE(SUM(lines), 0) FROM knowledge_churn "
            "WHERE repo = ? AND path = ? AND at > ?",
            (unit.repo, unit.path, _time(since)),
        ).fetchone()
        return int(row[0])

    def known_repos(self) -> set[str]:
        """Repos whose history was read; an exposure alone (a round, a KB page) doesn't make
        the rest of a repo known (M5). An override is the author's word: it does."""
        known = {str(repo) for (repo,) in self._conn.execute("SELECT repo FROM knowledge_repos")}
        overridden = (_unit(text) for (text,) in self._conn.execute(
            "SELECT unit FROM knowledge_overrides"
        ))  # fmt: skip
        return known | {u.repo for u in overridden if u is not None and u.repo is not None}

    # --- sources -------------------------------------------------------------------------

    def _add(self, source: str, rows: Sequence[ExposureRow], now: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO knowledge_sources (source, recorded_at) VALUES (?, ?)",
            (source, now),
        )
        if cursor.rowcount == 0:
            return False
        self._conn.executemany(
            "INSERT OR IGNORE INTO knowledge_exposures (source, unit, kind, at, repo, path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    source,
                    unit.id,
                    kind,
                    _time(at),
                    unit.repo,
                    None if unit.repo is None else unit.path,
                )
                for unit, kind, at in rows
            ],
        )
        return True

    def add_exposures(self, source: str, rows: Sequence[ExposureRow]) -> bool:
        with transaction(self._conn):
            return self._add(source, rows, _time(datetime.now(UTC)))

    def repo_state(self, repo: str) -> RepoState | None:
        row = self._conn.execute(
            "SELECT tip, emails FROM knowledge_repos WHERE repo = ?", (repo,)
        ).fetchone()
        return None if row is None else RepoState(row[0], row[1])

    def add_churn(self, repo: str, churn: Sequence[ChurnRow]) -> None:
        with transaction(self._conn):
            self._conn.executemany(
                "INSERT OR IGNORE INTO knowledge_churn (repo, sha, path, at, lines) "
                "VALUES (?, ?, ?, ?, ?)",
                [(repo, sha, path, _time(at), lines) for sha, at, path, lines in churn],
            )

    def record_commits(
        self, repo: str, state: RepoState, mine: Mapping[str, Sequence[ExposureRow]]
    ) -> int:
        now = _time(datetime.now(UTC))
        with transaction(self._conn):
            added = sum(self._add(source, rows, now) for source, rows in mine.items())
            self._conn.execute(
                "INSERT INTO knowledge_repos (repo, tip, emails, read_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (repo) DO UPDATE SET tip = excluded.tip, emails = excluded.emails, "
                "read_at = excluded.read_at",
                (repo, state.tip, state.emails, now),
            )
        return added

    # --- rounds and quizzes ---------------------------------------------------------------

    def pending_rounds(self) -> list[PendingRound]:
        """Rounds the author posted or marked done, not yet counted: at the posting's time
        when there is one, else when the round finished. Their paths are the ones stored
        with the round (I3: they outlive the PR's place in the queue), else, for a round
        from before they were stored, its PR's cached file list."""
        rows = self._conn.execute(
            "SELECT r.id, r.repo, r.depth, COALESCE(("
            "SELECT MAX(p.started_at) FROM postings p "
            "WHERE p.round_id = r.id AND p.state = 'posted'), r.finished_at, r.started_at), "
            "a.json, f.json FROM rounds r "
            "LEFT JOIN artifacts a ON a.round_id = r.id AND a.kind = 'paths' "
            "LEFT JOIN pr_files f "
            "ON f.repo = r.repo AND f.number = r.number AND f.head_sha = r.head_sha "
            "WHERE r.done = 1 AND NOT EXISTS ("
            "SELECT 1 FROM knowledge_sources s WHERE s.source = 'round:' || r.id) "
            "ORDER BY r.id"
        )
        return [
            PendingRound(
                round_id,
                repo,
                "review_deep" if depth == "deep" else "review_quick",
                _parse(at),
                _paths(paths, files),
            )
            for round_id, repo, depth, at, paths, files in rows
        ]

    def pending_quizzes(self) -> list[PendingRound]:
        rows = self._conn.execute(
            "SELECT q.round_id, r.repo, q.correct, q.answered, q.recorded_at, a.json, f.json "
            "FROM quiz_results q JOIN rounds r ON r.id = q.round_id "
            "LEFT JOIN artifacts a ON a.round_id = r.id AND a.kind = 'paths' "
            "LEFT JOIN pr_files f "
            "ON f.repo = r.repo AND f.number = r.number AND f.head_sha = r.head_sha "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM knowledge_sources s WHERE s.source = 'quiz:' || q.round_id) "
            "ORDER BY q.round_id"
        )
        return [
            PendingRound(
                round_id, repo, quiz_kind(correct, answered), _parse(at), _paths(paths, files)
            )
            for round_id, repo, correct, answered, at, paths, files in rows
        ]

    def round_pr(self, round_id: int) -> PrRef | None:
        row = self._conn.execute(
            "SELECT repo, number FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
        return None if row is None else PrRef(row[0], row[1])

    def add_quiz(self, round_id: int, right: int, answered: int, at: datetime) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO quiz_results (round_id, correct, answered, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (round_id, right, answered, _time(at)),
        )
        return cursor.rowcount == 1

    # --- KB pages ---------------------------------------------------------------------------

    def page_revs(self) -> dict[str, str]:
        return dict(self._conn.execute("SELECT page_id, rev FROM knowledge_pages").fetchall())

    def record_page(self, page_id: str, rev: str, rows: Sequence[ExposureRow]) -> bool:
        source = f"kb:{page_id}"
        with transaction(self._conn):
            # One set per page (M8): a new revision replaces the last one's exposures.
            self._conn.execute("DELETE FROM knowledge_sources WHERE source = ?", (source,))
            added = bool(rows) and self._add(source, rows, _time(datetime.now(UTC)))
            self._conn.execute(
                "INSERT INTO knowledge_pages (page_id, rev) VALUES (?, ?) "
                "ON CONFLICT (page_id) DO UPDATE SET rev = excluded.rev",
                (page_id, rev),
            )
        return added

    def prune(self, before: datetime) -> None:
        cutoff = _time(before)
        with transaction(self._conn):
            self._conn.execute("DELETE FROM knowledge_churn WHERE at < ?", (cutoff,))
            # The source rows stay, so a pruned signal is never ingested again.
            self._conn.execute("DELETE FROM knowledge_exposures WHERE at < ?", (cutoff,))


__all__ = ["SqliteKnowledgeStore"]
