# SPDX-License-Identifier: AGPL-3.0-or-later
"""``QueueStore`` over the module's own ``review.sqlite`` connection (autocommit, from
``open_db``). Times are stored as ISO 8601 text in UTC."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import cast

from jesinia.apps.review.domain.gates import CiState, Depth
from jesinia.apps.review.domain.jobs import DEFAULT_RUNNER
from jesinia.apps.review.domain.queue import (
    DEFAULT_PREREVIEW,
    AutoReviewState,
    PreReviewMode,
    PreReviewState,
    PrKind,
    PrRef,
    PrSnapshot,
    RepoConfig,
    RoundStatus,
    RoundSummary,
    StoredPr,
    coverage_from_json,
    evidence_from_json,
    parse_prereview_mode,
)
from jesinia.apps.review.domain.standards import Override
from jesinia.core.db import transaction
from jesinia.services.github.domain.pr import PrFile

_PREREVIEW_KEY = "prereview"
_STANDARDS_DIR_KEY = "standards.dir"
_STANDARDS_USER_DIR_KEY = "standards.user_dir"
_JOB_RUNNER_KEY = "jobs.runner"
_VIEWER_KEY = "viewer"
_SYNC_KEY = "queue.sync"

_SNAPSHOT_COLUMNS = (
    "repo",
    "number",
    "display_repo",
    "title",
    "author",
    "author_is_bot",
    "url",
    "head_sha",
    "base_sha",
    "base_ref",
    "head_ref",
    "head_repo",
    "created_at",
    "size_lines",
    "files",
    "review_requested",
    "ci_state",
    "auto_review_state",
    "review_comment_count",
    "bot_review_count",
    "human_review_count",
    "depth",
)
_LOCAL_COLUMNS = (
    "kind",
    "state",
    "state_reason",
    "last_head_change_at",
    "loaded",
    "dismissed",
    "fetch_error",
    "fetch_error_at",
)
_SELECT_PR = f"SELECT {', '.join(_SNAPSHOT_COLUMNS + _LOCAL_COLUMNS)} FROM prs"
# Columns a fresh fetch replaces; the depth and the state only change with the head.
_REFRESHED = [c for c in _SNAPSHOT_COLUMNS if c not in {"repo", "number", "depth"}]
_HEAD_MOVED = "prs.head_sha IS NOT excluded.head_sha"
_UPSERT_FRESH = f"""
INSERT INTO prs ({", ".join(_SNAPSHOT_COLUMNS)}, last_head_change_at, state, state_reason)
VALUES ({", ".join("?" for _ in _SNAPSHOT_COLUMNS)}, ?, 'debouncing', 'new head')
ON CONFLICT (repo, number) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in _REFRESHED)},
    depth = CASE WHEN {_HEAD_MOVED} THEN excluded.depth ELSE prs.depth END,
    last_head_change_at = CASE WHEN {_HEAD_MOVED}
        THEN excluded.last_head_change_at ELSE prs.last_head_change_at END,
    state = CASE WHEN {_HEAD_MOVED} THEN 'debouncing' ELSE prs.state END,
    state_reason = CASE WHEN {_HEAD_MOVED} THEN 'new head' ELSE prs.state_reason END,
    fetch_error = NULL,
    fetch_error_at = NULL
"""
# A PR GitHub couldn't load: never touch what the cache already knows about it.
_UPSERT_FAILED = f"""
INSERT INTO prs ({", ".join(_SNAPSHOT_COLUMNS)}, last_head_change_at, fetch_error,
                 fetch_error_at, state, state_reason)
VALUES ({", ".join("?" for _ in _SNAPSHOT_COLUMNS)}, ?, ?, ?, 'fetch_failed', '')
ON CONFLICT (repo, number) DO UPDATE SET
    fetch_error = excluded.fetch_error,
    fetch_error_at = excluded.fetch_error_at
"""

# The row shape ``_SELECT_PR`` returns.
_PrRow = tuple[
    str, int, str, str, str, int, str, str, str, str, str, str | None, str, int, int, int,
    str, str, int, int, int, str, str, str, str, str, int, int, str | None, str | None,
]  # fmt: skip


def _time(value: datetime) -> str:
    return value.isoformat()


def _optional_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _snapshot_values(pr: PrSnapshot) -> tuple[object, ...]:
    return (
        pr.ref.repo,
        pr.ref.number,
        pr.display_repo,
        pr.title,
        pr.author,
        int(pr.author_is_bot),
        pr.url,
        pr.head_sha,
        pr.base_sha,
        pr.base_ref,
        pr.head_ref,
        pr.head_repo,
        _time(pr.created_at),
        pr.size_lines,
        pr.files,
        int(pr.review_requested),
        pr.ci_state,
        pr.auto_review_state,
        pr.review_comment_count,
        pr.bot_review_count,
        pr.human_review_count,
        pr.depth,
    )


def _stored(row: _PrRow) -> StoredPr:
    (repo, number, display_repo, title, author, author_is_bot, url, head_sha, base_sha,
     base_ref, head_ref, head_repo, created_at, size_lines, files, review_requested, ci_state,
     auto_review_state, comments, bot_reviews, human_reviews, depth, kind, state, state_reason,
     last_head_change_at, loaded, dismissed, fetch_error, fetch_error_at) = row  # fmt: skip
    snapshot = PrSnapshot(
        ref=PrRef(repo, number),
        display_repo=display_repo,
        title=title,
        author=author,
        author_is_bot=bool(author_is_bot),
        url=url,
        head_sha=head_sha,
        base_sha=base_sha,
        base_ref=base_ref,
        head_ref=head_ref,
        head_repo=head_repo,
        created_at=datetime.fromisoformat(created_at),
        size_lines=size_lines,
        files=files,
        review_requested=bool(review_requested),
        # The CHECK-free text columns hold only values this store wrote from these types.
        ci_state=cast(CiState, ci_state),
        auto_review_state=cast(AutoReviewState, auto_review_state),
        review_comment_count=comments,
        bot_review_count=bot_reviews,
        human_review_count=human_reviews,
        depth=cast(Depth, depth),
    )
    return StoredPr(
        pr=snapshot,
        kind=cast(PrKind, kind),
        state=cast(PreReviewState, state),
        state_reason=state_reason,
        last_head_change_at=datetime.fromisoformat(last_head_change_at),
        loaded=bool(loaded),
        dismissed=bool(dismissed),
        fetch_error=fetch_error,
        fetch_error_at=_optional_time(fetch_error_at),
    )


def _file(data: object) -> PrFile:
    fields = cast(dict[str, object], data)
    return PrFile(
        filename=str(fields["filename"]),
        status=str(fields["status"]),
        additions=cast(int, fields["additions"]),
        deletions=cast(int, fields["deletions"]),
        patch=cast(str | None, fields["patch"]),
        previous_filename=cast(str | None, fields["previous_filename"]),
    )


class SqliteReviewStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def atomic(self) -> AbstractContextManager[sqlite3.Connection]:
        return transaction(self._conn)

    # Settings ---------------------------------------------------------------------------

    def _setting(self, key: str) -> object:
        row = self._conn.execute("SELECT json FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def _put_setting(self, key: str, value: object) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, json) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET json = excluded.json",
            (key, json.dumps(value)),
        )

    def prereview_default(self) -> PreReviewMode:
        value = self._setting(_PREREVIEW_KEY)
        return parse_prereview_mode(value) if isinstance(value, str) else DEFAULT_PREREVIEW

    def set_prereview_default(self, mode: PreReviewMode) -> None:
        self._put_setting(_PREREVIEW_KEY, parse_prereview_mode(mode))

    def viewer(self) -> str | None:
        value = self._setting(_VIEWER_KEY)
        return value if isinstance(value, str) else None

    def set_viewer(self, login: str) -> None:
        self._put_setting(_VIEWER_KEY, login)

    def _path_setting(self, key: str) -> Path | None:
        value = self._setting(key)
        return Path(value) if isinstance(value, str) and value else None

    def standards_dir(self) -> Path | None:
        """The author's coding-standards checkout (the base layer), if set."""
        return self._path_setting(_STANDARDS_DIR_KEY)

    def set_standards_dir(self, path: Path | None) -> None:
        self._put_setting(_STANDARDS_DIR_KEY, None if path is None else str(path))

    def standards_user_dir(self) -> Path | None:
        """The folder of the author's own standards documents (the user layer), if set."""
        return self._path_setting(_STANDARDS_USER_DIR_KEY)

    def set_standards_user_dir(self, path: Path | None) -> None:
        self._put_setting(_STANDARDS_USER_DIR_KEY, None if path is None else str(path))

    def job_runner(self) -> str:
        """The agent runner review jobs use (``codex`` unless set)."""
        value = self._setting(_JOB_RUNNER_KEY)
        return value if isinstance(value, str) and value else DEFAULT_RUNNER

    def set_job_runner(self, runner: str) -> None:
        self._put_setting(_JOB_RUNNER_KEY, runner)

    def standards_overrides(self, repo: str) -> list[Override]:
        """The author's own overrides, then ``repo``'s, each in the order they were added."""
        rows = self._conn.execute(
            "SELECT doc, heading_path, text FROM standards_overrides "
            "WHERE repo IS NULL OR repo = ? ORDER BY repo IS NOT NULL, id",
            (RepoConfig(repo).repo,),
        )
        return [Override(doc, tuple(json.loads(path)), text) for doc, path, text in rows]

    def add_standards_override(self, repo: str | None, override: Override) -> None:
        """Store an override for ``repo``, or for every repo when ``repo`` is ``None``."""
        self._conn.execute(
            "INSERT INTO standards_overrides (repo, doc, heading_path, text) VALUES (?, ?, ?, ?)",
            (
                None if repo is None else RepoConfig(repo).repo,
                override.doc,
                json.dumps(list(override.heading_path)),
                override.text,
            ),
        )

    def sync_status(self) -> tuple[datetime | None, str | None]:
        value = self._setting(_SYNC_KEY)
        if not isinstance(value, dict):
            return None, None
        synced_at, error = value.get("synced_at"), value.get("error")
        return (
            datetime.fromisoformat(synced_at) if isinstance(synced_at, str) else None,
            error if isinstance(error, str) else None,
        )

    def record_sync(self, synced_at: datetime | None, error: str | None) -> None:
        last_good = synced_at or self.sync_status()[0]
        self._put_setting(
            _SYNC_KEY,
            {"synced_at": None if last_good is None else _time(last_good), "error": error},
        )

    # Repos ------------------------------------------------------------------------------

    def watched_repos(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT repo FROM repos WHERE watched = 1 AND suggested = 0 ORDER BY repo"
        )
        return [repo for (repo,) in rows]

    def _add_repos(self, repos: Iterable[str], *, watched: bool) -> list[str]:
        added: list[str] = []
        for repo in repos:
            canonical = RepoConfig(repo).repo
            cursor = self._conn.execute(
                "INSERT INTO repos (repo, watched, suggested) VALUES (?, ?, ?) "
                "ON CONFLICT (repo) DO NOTHING",
                (canonical, int(watched), int(not watched)),
            )
            if cursor.rowcount:
                added.append(canonical)
        return added

    def seed_repos(self, repos: Iterable[str]) -> list[str]:
        return self._add_repos(repos, watched=True)

    def suggest_repos(self, repos: Iterable[str]) -> list[str]:
        return self._add_repos(repos, watched=False)

    def suggested_repos(self) -> list[str]:
        rows = self._conn.execute("SELECT repo FROM repos WHERE suggested = 1 ORDER BY repo")
        return [repo for (repo,) in rows]

    def confirm_repo(self, repo: str) -> None:
        self._conn.execute(
            "INSERT INTO repos (repo, watched, suggested) VALUES (?, 1, 0) "
            "ON CONFLICT (repo) DO UPDATE SET watched = 1, suggested = 0",
            (RepoConfig(repo).repo,),
        )

    def repo_configs(self) -> dict[str, RepoConfig]:
        rows = self._conn.execute(
            "SELECT repo, watched, automated_reviewer, prereview, suggested FROM repos"
        )
        return {
            repo: RepoConfig(
                repo,
                watched=bool(watched),
                automated_reviewer=reviewer,
                prereview=None if prereview is None else parse_prereview_mode(prereview),
                suggested=bool(suggested),
            )
            for repo, watched, reviewer, prereview, suggested in rows
        }

    def upsert_repo(self, config: RepoConfig) -> None:
        self._conn.execute(
            "INSERT INTO repos (repo, watched, automated_reviewer, prereview, suggested) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (repo) DO UPDATE SET watched = excluded.watched, "
            "automated_reviewer = excluded.automated_reviewer, prereview = excluded.prereview, "
            "suggested = excluded.suggested",
            (
                config.repo,
                int(config.watched),
                config.automated_reviewer,
                config.prereview,
                int(config.suggested),
            ),
        )

    # PRs --------------------------------------------------------------------------------

    def pr(self, ref: PrRef) -> StoredPr | None:
        row = self._conn.execute(
            f"{_SELECT_PR} WHERE repo = ? AND number = ?", (ref.repo, ref.number)
        ).fetchone()
        return None if row is None else _stored(row)

    def prs(self, include_dismissed: bool = False) -> list[StoredPr]:
        where = "" if include_dismissed else " WHERE dismissed = 0"
        return [_stored(row) for row in self._conn.execute(f"{_SELECT_PR}{where}")]

    def apply_sync(
        self,
        fresh: Sequence[PrSnapshot],
        failed: Sequence[tuple[PrSnapshot, str]],
        removed: Sequence[PrRef],
        now: datetime,
    ) -> None:
        with transaction(self._conn):
            for pr in fresh:
                self._conn.execute(_UPSERT_FRESH, (*_snapshot_values(pr), _time(now)))
            for pr, error in failed:
                values = (*_snapshot_values(pr), _time(now), error, _time(now))
                self._conn.execute(_UPSERT_FAILED, values)
            for ref in removed:
                for table in ("prs", "pr_files"):
                    self._conn.execute(
                        f"DELETE FROM {table} WHERE repo = ? AND number = ?",
                        (ref.repo, ref.number),
                    )

    def _flag(self, ref: PrRef, loaded: bool) -> bool:
        cursor = self._conn.execute(
            "UPDATE prs SET loaded = ?, dismissed = ? WHERE repo = ? AND number = ?",
            (int(loaded), int(not loaded), ref.repo, ref.number),
        )
        return cursor.rowcount > 0

    def set_loaded(self, ref: PrRef) -> StoredPr | None:
        return self.pr(ref) if self._flag(ref, loaded=True) else None

    def add_loaded(self, snapshot: PrSnapshot, now: datetime) -> StoredPr:
        with transaction(self._conn):
            self._conn.execute(_UPSERT_FRESH, (*_snapshot_values(snapshot), _time(now)))
            self._flag(snapshot.ref, loaded=True)
            row = self._conn.execute(
                f"{_SELECT_PR} WHERE repo = ? AND number = ?",
                (snapshot.ref.repo, snapshot.ref.number),
            ).fetchone()
        return _stored(row)

    def set_dismissed(self, ref: PrRef) -> bool:
        return self._flag(ref, loaded=False)

    def set_state(self, ref: PrRef, head_sha: str, state: PreReviewState, reason: str) -> None:
        self._conn.execute(
            "UPDATE prs SET state = ?, state_reason = ? "
            "WHERE repo = ? AND number = ? AND head_sha = ? "
            "AND (state IS NOT ? OR state_reason IS NOT ?)",
            (state, reason, ref.repo, ref.number, head_sha, state, reason),
        )

    # Rounds -----------------------------------------------------------------------------

    def start_round(
        self, ref: PrRef, head_sha: str, base_sha: str, depth: Depth, started_at: datetime
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO rounds (repo, number, head_sha, base_sha, depth, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ref.repo, ref.number, head_sha, base_sha, depth, _time(started_at)),
        )
        return cast(int, cursor.lastrowid)

    def finish_round(
        self,
        round_id: int,
        finished_at: datetime,
        error: str | None,
        verdict: str | None = None,
        *,
        interrupted: bool = False,
    ) -> None:
        self._conn.execute(
            "UPDATE rounds SET finished_at = ?, error = ?, verdict = COALESCE(?, verdict), "
            "interrupted = ? WHERE id = ?",
            (_time(finished_at), error, verdict, int(interrupted), round_id),
        )

    def mark_round_done(self, round_id: int) -> None:
        self._conn.execute("UPDATE rounds SET done = 1 WHERE id = ?", (round_id,))

    def interrupt_unfinished_rounds(self, at: datetime, cause: str) -> int:
        cursor = self._conn.execute(
            "UPDATE rounds SET finished_at = ?, error = ?, interrupted = 1 "
            "WHERE finished_at IS NULL",
            (_time(at), cause),
        )
        return cursor.rowcount

    def rounds_for_heads(self) -> dict[tuple[PrRef, str], RoundStatus]:
        rows = self._conn.execute(
            "SELECT r.repo, r.number, r.head_sha, r.finished_at IS NOT NULL, r.error, "
            "r.interrupted, c.interruptions FROM rounds r JOIN ("
            "SELECT MAX(id) AS id, SUM(interrupted) AS interruptions FROM rounds "
            "GROUP BY repo, number, head_sha) c ON c.id = r.id"
        )
        return {
            (PrRef(repo, number), head): RoundStatus(
                bool(finished), error, bool(interrupted), interruptions
            )
            for repo, number, head, finished, error, interrupted, interruptions in rows
        }

    def latest_rounds(self) -> dict[PrRef, RoundSummary]:
        rows = self._conn.execute(
            "SELECT r.repo, r.number, r.head_sha, r.verdict, e.json, c.json FROM rounds r "
            "LEFT JOIN artifacts e ON e.round_id = r.id AND e.kind = 'evidence' "
            "LEFT JOIN artifacts c ON c.round_id = r.id AND c.kind = 'coverage' "
            "WHERE r.id IN (SELECT MAX(id) FROM rounds "
            "WHERE finished_at IS NOT NULL AND error IS NULL GROUP BY repo, number)"
        )
        return {
            PrRef(repo, number): RoundSummary(
                head_sha=head,
                verdict=verdict,
                evidence=None if evidence is None else evidence_from_json(json.loads(evidence)),
                coverage_pct=None if coverage is None else coverage_from_json(json.loads(coverage)),
            )
            for repo, number, head, verdict, evidence, coverage in rows
        }

    def latest_round_on_head(self, ref: PrRef, head_sha: str) -> tuple[int, RoundStatus] | None:
        """The newest round on ``(ref, head_sha)`` with its status, or ``None``."""
        row = self._conn.execute(
            "SELECT id, finished_at IS NOT NULL, error, interrupted FROM rounds "
            "WHERE repo = ? AND number = ? AND head_sha = ? ORDER BY id DESC LIMIT 1",
            (ref.repo, ref.number, head_sha),
        ).fetchone()
        if row is None:
            return None
        round_id, finished, error, interrupted = row
        return round_id, RoundStatus(bool(finished), error, bool(interrupted))

    def set_depth(self, ref: PrRef, head_sha: str, round_id: int, depth: Depth) -> None:
        """Record the depth a round runs at, on the round and (while the PR is still at
        ``head_sha``) on its queue row."""
        with transaction(self._conn):
            self._conn.execute("UPDATE rounds SET depth = ? WHERE id = ?", (depth, round_id))
            self._conn.execute(
                "UPDATE prs SET depth = ? WHERE repo = ? AND number = ? AND head_sha = ?",
                (depth, ref.repo, ref.number, head_sha),
            )

    def set_verdict(self, round_id: int, verdict: str) -> None:
        self._conn.execute("UPDATE rounds SET verdict = ? WHERE id = ?", (verdict, round_id))

    def artifacts(self, round_id: int) -> dict[str, object]:
        """Every artifact of a round, by kind."""
        rows = self._conn.execute(
            "SELECT kind, json FROM artifacts WHERE round_id = ?", (round_id,)
        )
        return {kind: json.loads(data) for kind, data in rows}

    def put_artifact(self, round_id: int, kind: str, data: object) -> None:
        self._conn.execute(
            "INSERT INTO artifacts (round_id, kind, json) VALUES (?, ?, ?) "
            "ON CONFLICT (round_id, kind) DO UPDATE SET json = excluded.json",
            (round_id, kind, json.dumps(data)),
        )

    # Files and the prune ----------------------------------------------------------------

    def pr_files(self, ref: PrRef, head_sha: str) -> list[PrFile] | None:
        row = self._conn.execute(
            "SELECT json FROM pr_files WHERE repo = ? AND number = ? AND head_sha = ?",
            (ref.repo, ref.number, head_sha),
        ).fetchone()
        return None if row is None else [_file(item) for item in json.loads(row[0])]

    def put_pr_files(self, ref: PrRef, head_sha: str, files: Sequence[PrFile]) -> None:
        self._conn.execute(
            "INSERT INTO pr_files (repo, number, head_sha, json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (repo, number, head_sha) DO UPDATE SET json = excluded.json",
            (ref.repo, ref.number, head_sha, json.dumps([asdict(f) for f in files])),
        )

    def keep_shas(self) -> set[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT repo, head_sha, base_sha FROM prs WHERE dismissed = 0 "
            "UNION ALL "
            "SELECT r.repo, r.head_sha, r.base_sha FROM rounds r "
            "JOIN prs p ON p.repo = r.repo AND p.number = r.number "
            "WHERE p.dismissed = 0 AND (r.finished_at IS NULL OR (r.done = 0 AND r.id IN ("
            "SELECT MAX(id) FROM rounds WHERE finished_at IS NOT NULL GROUP BY repo, number)))"
        )
        return {(repo, sha) for repo, head, base in rows for sha in (head, base) if sha}
