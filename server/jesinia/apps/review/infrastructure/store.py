# SPDX-License-Identifier: AGPL-3.0-or-later
"""``QueueStore`` over the module's own ``review.sqlite`` connection (autocommit, from
``open_db``). Times are stored as ISO 8601 text in UTC."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import cast

from jesinia.apps.review.domain.coverage import with_clean_titles
from jesinia.apps.review.domain.followup import FollowUp, FollowUpKind, PrevSource
from jesinia.apps.review.domain.gates import CiState, Depth
from jesinia.apps.review.domain.jobs import DEFAULT_RUNNER
from jesinia.apps.review.domain.posting import (
    DRAFTS_MAX,
    FINDING_KINDS,
    Draft,
    Posting,
    PostingState,
    RoundFrozen,
    artifact_drafts,
)
from jesinia.apps.review.domain.queue import (
    DEFAULT_PREREVIEW,
    AutoReviewState,
    InvalidSettingError,
    PreReviewMode,
    PreReviewState,
    PrKind,
    PrRef,
    PrSnapshot,
    RepoConfig,
    RoundRecord,
    RoundStatus,
    RoundSummary,
    StoredPr,
    coverage_from_json,
    evidence_from_json,
    nothing_to_walk_from_json,
    parse_metric_criteria,
    parse_prereview_mode,
)
from jesinia.apps.review.domain.standards import Override
from jesinia.core.db import transaction
from jesinia.services.github.domain.pr import PrFile, ReviewEvent, ReviewSide

log = logging.getLogger(__name__)

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
    "viewer_review",
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
    str, str, int, int, int, str, str, str, str, str, str, int, int, str | None, str | None,
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
        pr.viewer_review,
    )


def _stored(row: _PrRow) -> StoredPr:
    (repo, number, display_repo, title, author, author_is_bot, url, head_sha, base_sha,
     base_ref, head_ref, head_repo, created_at, size_lines, files, review_requested, ci_state,
     auto_review_state, comments, bot_reviews, human_reviews, depth, viewer_review, kind, state,
     state_reason, last_head_change_at, loaded, dismissed, fetch_error,
     fetch_error_at) = row  # fmt: skip
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
        viewer_review=viewer_review,
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


_ROUND_COLUMNS = (
    "id, repo, number, head_sha, base_sha, depth, started_at, finished_at, verdict, error, "
    "posted_review_url, done, frozen_at, successor_id, interrupted"
)
_DRAFT_COLUMNS = (
    "id, path, line, side, start_line, start_side, body, origin_finding, outdated, outside_diff"
)
_POSTING_COLUMNS = "id, round_id, event, key, digest, body, started_at, state, review_url, error"


def _round(row: tuple[object, ...]) -> RoundRecord:
    (round_id, repo, number, head, base, depth, started, finished, verdict, error, url, done,
     frozen, successor, interrupted) = row  # fmt: skip
    return RoundRecord(
        id=cast(int, round_id),
        ref=PrRef(cast(str, repo), cast(int, number)),
        head_sha=cast(str, head),
        base_sha=cast(str, base),
        depth=cast(Depth, depth),
        started_at=datetime.fromisoformat(cast(str, started)),
        finished_at=_optional_time(cast(str | None, finished)),
        verdict=cast(str | None, verdict),
        error=cast(str | None, error),
        posted_review_url=cast(str | None, url),
        done=bool(done),
        frozen_at=_optional_time(cast(str | None, frozen)),
        successor_id=cast(int | None, successor),
        interrupted=bool(interrupted),
    )


def _draft(row: tuple[object, ...]) -> Draft:
    draft_id, path, line, side, start_line, start_side, body, origin, outdated, outside = row
    return Draft(
        path=cast(str, path),
        body=cast(str, body),
        line=cast(int | None, line),
        side=cast(ReviewSide | None, side),
        start_line=cast(int | None, start_line),
        start_side=cast(ReviewSide | None, start_side),
        origin_finding=cast(str | None, origin),
        outdated=bool(outdated),
        outside_diff=bool(outside),
        id=cast(int, draft_id),
    )


def _posting(row: tuple[object, ...]) -> Posting:
    posting_id, round_id, event, key, digest, body, started, state, url, error = row
    return Posting(
        id=cast(int, posting_id),
        round_id=cast(int, round_id),
        event=cast(ReviewEvent, event),
        key=cast(str, key),
        digest=cast(str, digest),
        body=cast(str, body),
        started_at=datetime.fromisoformat(cast(str, started)),
        state=cast(PostingState, state),
        review_url=cast(str | None, url),
        error=cast(str | None, error),
    )


_FOLLOWUP_COLUMNS = (
    "kind, differing_paths, note, prev_head, prev_source, error, prev_merge_base, prev_round, "
    "review_key"
)


def _followup(row: tuple[object, ...]) -> FollowUp:
    kind, paths, note, prev_head, prev_source, error, prev_merge_base, prev_round, key = row
    return FollowUp(
        kind=cast(FollowUpKind, kind),
        differing_paths=list(json.loads(cast(str, paths))),
        note=cast(str, note),
        prev_head=cast(str | None, prev_head),
        prev_source=cast(PrevSource | None, prev_source),
        error=cast(str | None, error),
        prev_merge_base=cast(str | None, prev_merge_base),
        prev_round_id=cast(int | None, prev_round),
        review_key=cast(str, key),
    )


def _stored_metrics(repo: str, stored: str | None) -> tuple[str, ...] | None:
    """A repo's stored metric set; one that can't be read (or no longer passes the rules)
    becomes ``None``, the standards' criteria, with the cause logged: one bad row never stops
    the queue, the pipeline or Settings from reading every repo."""
    if stored is None:
        return None
    try:
        values = json.loads(stored)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise InvalidSettingError("the stored metric set is not a list of text")
        return parse_metric_criteria(values)
    except (ValueError, InvalidSettingError) as exc:
        log.warning("review: %s's metric set is unreadable, so the standards decide: %s", repo, exc)
        return None


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
            "SELECT repo, watched, automated_reviewer, prereview, suggested, metrics FROM repos"
        )
        return {
            repo: RepoConfig(
                repo,
                watched=bool(watched),
                automated_reviewer=reviewer,
                prereview=None if prereview is None else parse_prereview_mode(prereview),
                suggested=bool(suggested),
                metrics=_stored_metrics(repo, metrics),
            )
            for repo, watched, reviewer, prereview, suggested, metrics in rows
        }

    def upsert_repo(self, config: RepoConfig) -> None:
        metrics = None if config.metrics is None else json.dumps(list(config.metrics))
        self._conn.execute(
            "INSERT INTO repos (repo, watched, automated_reviewer, prereview, suggested, metrics) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repo) DO UPDATE SET watched = excluded.watched, "
            "automated_reviewer = excluded.automated_reviewer, prereview = excluded.prereview, "
            "suggested = excluded.suggested, metrics = excluded.metrics",
            (
                config.repo,
                int(config.watched),
                config.automated_reviewer,
                config.prereview,
                int(config.suggested),
                metrics,
            ),
        )

    def metric_rounds(self, repo: str, limit: int) -> list[tuple[int, int, str, datetime, object]]:
        """The latest ``limit`` rounds of ``repo`` (canonical) with a ``metrics`` artifact,
        oldest first: id, PR number, head, start time and the decoded artifact."""
        rows = self._conn.execute(
            "SELECT r.id, r.number, r.head_sha, r.started_at, a.json FROM rounds r "
            "JOIN artifacts a ON a.round_id = r.id AND a.kind = 'metrics' "
            "WHERE r.repo = ? ORDER BY r.id DESC LIMIT ?",
            (repo, limit),
        ).fetchall()
        return [
            (round_id, number, head, datetime.fromisoformat(started), json.loads(data))
            for round_id, number, head, started, data in reversed(rows)
        ]

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
            # Follow-ups are per current head (M6) and per the author's latest review (R75):
            # a moved or removed PR's old rows go, and so does one classified against a
            # review that is no longer the author's latest (it is classified again).
            self._conn.execute(
                "DELETE FROM followups WHERE NOT EXISTS (SELECT 1 FROM prs p "
                "WHERE p.repo = followups.repo AND p.number = followups.number "
                "AND p.head_sha = followups.head_sha "
                "AND (followups.error IS NOT NULL OR p.viewer_review = followups.review_key))"
            )
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
        """Start a round; in the same transaction the PR's latest round is frozen and its
        summary and unposted drafts move to the new one (spec §7), so no save can fall
        between the two."""
        with transaction(self._conn):
            row = self._conn.execute(
                f"SELECT {_ROUND_COLUMNS} FROM rounds WHERE repo = ? AND number = ? "
                "ORDER BY id DESC LIMIT 1",
                (ref.repo, ref.number),
            ).fetchone()
            cursor = self._conn.execute(
                "INSERT INTO rounds (repo, number, head_sha, base_sha, depth, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ref.repo, ref.number, head_sha, base_sha, depth, _time(started_at)),
            )
            round_id = cast(int, cursor.lastrowid)
            # The repo's metric set as it is now: the round is scored on it, whatever is saved
            # later (spec §5.2, "from the next round"). No repo row: the standards decide.
            self._conn.execute(
                "INSERT INTO artifacts (round_id, kind, json) "
                "SELECT ?, 'metric_criteria', COALESCE(metrics, 'null') FROM repos WHERE repo = ?",
                (round_id, ref.repo),
            )
            if row is not None:
                self._replace(_round(row), round_id, head_sha, started_at)
        return round_id

    def _replace(self, old: RoundRecord, new_id: int, head_sha: str, at: datetime) -> None:
        """Freeze ``old`` for round ``new_id`` and carry its summary and drafts over, unless
        a review of it was posted or may have been (a lost response): never posted twice.
        On the same head the drafts move as they are; on a new head every anchored draft is
        ``outdated``, remembering the head its lines refer to, until it is re-anchored.
        (The latest round is never frozen: only a newer round freezes one.)"""
        self._conn.execute(
            "UPDATE rounds SET frozen_at = ?, successor_id = ? WHERE id = ?",
            (_time(at), new_id, old.id),
        )
        posted = self._conn.execute(
            "SELECT 1 FROM postings WHERE round_id = ? "
            "AND state IN ('started', 'uncertain', 'posted') LIMIT 1",
            (old.id,),
        ).fetchone()
        if old.posted_review_url is not None or posted is not None:
            return
        self._copy_drafts(old, new_id, head_sha)

    def _copy_drafts(self, old: RoundRecord, new_id: int, head_sha: str) -> None:
        """Copy ``old``'s summary (where the new round has none) and drafts (not repeating
        one already there, same file and text) to round ``new_id`` on ``head_sha``."""
        self._conn.execute(
            "UPDATE rounds SET summary = (SELECT summary FROM rounds WHERE id = ?) "
            "WHERE id = ? AND summary = ''",
            (old.id, new_id),
        )
        same = old.head_sha == head_sha
        # An anchored draft becomes outdated; one already outdated by the author's own save
        # (no carried head) stays so, with nothing to re-anchor from.
        self._conn.execute(
            "INSERT INTO drafts (round_id, path, line, side, start_line, start_side, body, "
            "origin_finding, outdated, outside_diff, carried_head, carried_base) "
            "SELECT ?, path, line, side, start_line, start_side, body, origin_finding, "
            "CASE WHEN ? OR line IS NULL THEN outdated ELSE 1 END, outside_diff, "
            "CASE WHEN ? OR line IS NULL OR (outdated = 1 AND carried_head IS NULL) "
            "THEN carried_head ELSE COALESCE(carried_head, ?) END, "
            "CASE WHEN ? OR line IS NULL OR (outdated = 1 AND carried_head IS NULL) "
            "THEN carried_base ELSE COALESCE(carried_base, ?) END "
            "FROM drafts d WHERE round_id = ? AND NOT EXISTS (SELECT 1 FROM drafts n "
            "WHERE n.round_id = ? AND n.path = d.path AND n.body = d.body) ORDER BY id",
            (new_id, same, same, old.head_sha, same, old.base_sha, old.id, new_id),
        )

    def _release_held_drafts(self, round_id: int) -> None:
        """A frozen round whose drafts were held back (a posting of it might have landed)
        and whose every attempt is now known not to be on GitHub hands them to the PR's
        current round (I2). Never while any attempt is unsettled or posted."""
        row = self._conn.execute(
            f"SELECT {_ROUND_COLUMNS} FROM rounds WHERE id = ? AND frozen_at IS NOT NULL "
            "AND posted_review_url IS NULL AND NOT EXISTS (SELECT 1 FROM postings "
            "WHERE round_id = ? AND state IN ('started', 'uncertain', 'posted'))",
            (round_id, round_id),
        ).fetchone()
        if row is None:
            return
        old = _round(row)
        current = self._conn.execute(
            "SELECT id, head_sha FROM rounds WHERE repo = ? AND number = ? "
            "ORDER BY id DESC LIMIT 1",
            (old.ref.repo, old.ref.number),
        ).fetchone()
        self._copy_drafts(old, cast(int, current[0]), cast(str, current[1]))

    def _refuse_frozen(self, round_id: int) -> None:
        row = self._conn.execute(
            "SELECT frozen_at, successor_id FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if row is not None and row[0] is not None:
            raise RoundFrozen(round_id, cast(int | None, row[1]))

    def finish_round(
        self,
        round_id: int,
        finished_at: datetime,
        error: str | None,
        verdict: str | None = None,
        *,
        interrupted: bool = False,
        counted: bool = True,
    ) -> None:
        """End a round. ``interrupted``: cut short, to be resumed; ``counted``: the cut
        counts toward the head's limit (a stop or crash does, a head move or a transient
        GitHub failure doesn't)."""
        counts = int(interrupted and counted)
        self._conn.execute(
            "UPDATE rounds SET finished_at = ?, error = ?, verdict = COALESCE(?, verdict), "
            "interrupted = ?, interruptions = interruptions + ? WHERE id = ?",
            (_time(finished_at), error, verdict, int(interrupted), counts, round_id),
        )

    def resume_round(self, round_id: int) -> bool:
        """Reopen an interrupted round (R51): its artifacts, jobs and drafts stay, its
        interruptions keep counting. ``False`` when it isn't an interrupted round."""
        cursor = self._conn.execute(
            "UPDATE rounds SET finished_at = NULL, error = NULL, interrupted = 0 "
            "WHERE id = ? AND interrupted = 1",
            (round_id,),
        )
        return cursor.rowcount == 1

    def mark_round_done(self, round_id: int) -> None:
        """Raises: RoundFrozen: a newer round replaced it."""
        with transaction(self._conn):
            self._refuse_frozen(round_id)
            self._conn.execute("UPDATE rounds SET done = 1 WHERE id = ?", (round_id,))

    def interrupt_unfinished_rounds(self, at: datetime, cause: str) -> int:
        cursor = self._conn.execute(
            "UPDATE rounds SET finished_at = ?, error = ?, interrupted = 1, "
            "interruptions = interruptions + 1 WHERE finished_at IS NULL",
            (_time(at), cause),
        )
        return cursor.rowcount

    def rounds_for_heads(self) -> dict[tuple[PrRef, str], RoundStatus]:
        rows = self._conn.execute(
            "SELECT r.repo, r.number, r.head_sha, r.finished_at IS NOT NULL, r.error, "
            "r.interrupted, c.interruptions FROM rounds r JOIN ("
            "SELECT MAX(id) AS id, SUM(interruptions) AS interruptions FROM rounds "
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
                nothing_to_walk=coverage is not None
                and nothing_to_walk_from_json(json.loads(coverage)),
            )
            for repo, number, head, verdict, evidence, coverage in rows
        }

    def latest_round_on_head(self, ref: PrRef, head_sha: str) -> tuple[int, RoundStatus] | None:
        """The newest round on ``(ref, head_sha)`` with its status (interruptions counted
        over every round on the head), or ``None``."""
        row = self._conn.execute(
            "SELECT id, finished_at IS NOT NULL, error, interrupted, ("
            "SELECT SUM(interruptions) FROM rounds "
            "WHERE repo = ? AND number = ? AND head_sha = ?) FROM rounds "
            "WHERE repo = ? AND number = ? AND head_sha = ? ORDER BY id DESC LIMIT 1",
            (ref.repo, ref.number, head_sha) * 2,
        ).fetchone()
        if row is None:
            return None
        round_id, finished, error, interrupted, interruptions = row
        return round_id, RoundStatus(bool(finished), error, bool(interrupted), interruptions)

    def choose_depth(self, ref: PrRef, head_sha: str, depth: Depth) -> None:
        """Record the author's depth for a head (R57), and show it on the queue row while the
        PR is still at that head."""
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO depth_choices (repo, number, head_sha, depth) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (repo, number, head_sha) DO UPDATE SET depth = excluded.depth",
                (ref.repo, ref.number, head_sha, depth),
            )
            self._conn.execute(
                "UPDATE prs SET depth = ? WHERE repo = ? AND number = ? AND head_sha = ?",
                (depth, ref.repo, ref.number, head_sha),
            )

    def override_gates(self, ref: PrRef, head_sha: str) -> None:
        """Record the author's Review anyway for a head (R66)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO gate_overrides (repo, number, head_sha) VALUES (?, ?, ?)",
            (ref.repo, ref.number, head_sha),
        )

    def gates_overridden(self, ref: PrRef, head_sha: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM gate_overrides WHERE repo = ? AND number = ? AND head_sha = ?",
            (ref.repo, ref.number, head_sha),
        ).fetchone()
        return row is not None

    def chosen_depth(self, ref: PrRef, head_sha: str) -> Depth | None:
        row = self._conn.execute(
            "SELECT depth FROM depth_choices WHERE repo = ? AND number = ? AND head_sha = ?",
            (ref.repo, ref.number, head_sha),
        ).fetchone()
        return None if row is None else cast(Depth, row[0])

    def set_depth(self, ref: PrRef, head_sha: str, round_id: int, depth: Depth) -> None:
        """Record the depth a round runs at, on the round and (while the PR is still at
        ``head_sha``) on its queue row."""
        with transaction(self._conn):
            self._conn.execute("UPDATE rounds SET depth = ? WHERE id = ?", (depth, round_id))
            self._conn.execute(
                "UPDATE prs SET depth = ? WHERE repo = ? AND number = ? AND head_sha = ?",
                (depth, ref.repo, ref.number, head_sha),
            )

    def round(self, round_id: int) -> RoundRecord | None:
        row = self._conn.execute(
            f"SELECT {_ROUND_COLUMNS} FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
        return None if row is None else _round(row)

    def latest_round(self, ref: PrRef) -> RoundRecord | None:
        """The PR's newest round, however it ended (or while it runs)."""
        row = self._conn.execute(
            f"SELECT {_ROUND_COLUMNS} FROM rounds WHERE repo = ? AND number = ? "
            "ORDER BY id DESC LIMIT 1",
            (ref.repo, ref.number),
        ).fetchone()
        return None if row is None else _round(row)

    # Drafts and postings ----------------------------------------------------------------

    def drafts(self, round_id: int) -> list[Draft]:
        rows = self._conn.execute(
            f"SELECT {_DRAFT_COLUMNS} FROM drafts WHERE round_id = ? ORDER BY id", (round_id,)
        )
        return [_draft(row) for row in rows]

    def review_summary(self, round_id: int) -> str:
        row = self._conn.execute("SELECT summary FROM rounds WHERE id = ?", (round_id,)).fetchone()
        return "" if row is None else cast(str, row[0])

    def _insert_drafts(
        self,
        round_id: int,
        drafts: Sequence[Draft],
        carried: Sequence[tuple[str | None, str | None]] | None = None,
    ) -> None:
        origins = carried if carried is not None else [(None, None)] * len(drafts)
        self._conn.executemany(
            "INSERT INTO drafts (round_id, path, line, side, start_line, start_side, body, "
            "origin_finding, outdated, outside_diff, carried_head, carried_base) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    round_id,
                    d.path,
                    d.line,
                    d.side,
                    d.start_line,
                    d.start_side,
                    d.body,
                    d.origin_finding,
                    int(d.outdated),
                    int(d.outside_diff),
                    head,
                    base,
                )
                for d, (head, base) in zip(drafts, origins, strict=True)
            ],
        )

    def carried_drafts(self, round_id: int) -> list[tuple[Draft, str, str]]:
        """The round's drafts still to re-anchor, each with the head and base its lines
        refer to."""
        rows = self._conn.execute(
            f"SELECT {_DRAFT_COLUMNS}, carried_head, carried_base FROM drafts "
            "WHERE round_id = ? AND carried_head IS NOT NULL ORDER BY id",
            (round_id,),
        )
        return [(_draft(row[:-2]), cast(str, row[-2]), cast(str, row[-1])) for row in rows]

    def reanchor_drafts(self, round_id: int, drafts: Sequence[Draft]) -> int:
        """Store re-anchored drafts (by id: new lines and path, after a rename, or outdated)
        and clear their carried head; a draft the author saved since (no longer pending) is
        left as they saved it. Returns how many were updated."""
        updated = 0
        with transaction(self._conn):
            for d in drafts:
                cursor = self._conn.execute(
                    "UPDATE drafts SET path = ?, line = ?, start_line = ?, outdated = ?, "
                    "carried_head = NULL, carried_base = NULL "
                    "WHERE id = ? AND round_id = ? AND carried_head IS NOT NULL",
                    (d.path, d.line, d.start_line, int(d.outdated), d.id, round_id),
                )
                updated += cursor.rowcount
        return updated

    def seed_drafts(self, round_id: int, kind: str, drafts: Sequence[Draft]) -> list[Draft]:
        """Add the drafts generated from one kind of a round's findings, once per kind,
        after any already there (the author's, carried ones, another kind's: a finding
        repeating one, same file and text, is left out), as many as fit under DRAFTS_MAX.
        Returns the drafts added: none if that kind was seeded before or the round is
        frozen (the claim and the inserts are one transaction, so two readers can't both
        seed a kind). Drafts left out for lack of room are counted (``seeds_dropped``)."""
        with transaction(self._conn):
            if not self._claim_seeds(round_id, kind, drafts, self._draft_keys(round_id)):
                return []
            return self._add_seeds(round_id, drafts)

    def _draft_keys(self, round_id: int) -> set[tuple[str, str]]:
        rows = self._conn.execute("SELECT path, body FROM drafts WHERE round_id = ?", (round_id,))
        return {(cast(str, path), cast(str, body)) for path, body in rows}

    def _claim_seeds(
        self, round_id: int, kind: str, drafts: Sequence[Draft], before: set[tuple[str, str]]
    ) -> bool:
        """Claim seeding ``kind`` for an open round; True when its drafts are to be added
        (claimed now, and not seeded before 0011). ``before``: the drafts the round had."""
        claimed = self._conn.execute(
            "INSERT INTO draft_seeds (round_id, kind) "
            "SELECT id, ? FROM rounds WHERE id = ? AND frozen_at IS NULL "
            "ON CONFLICT (round_id, kind) DO NOTHING",
            (kind, round_id),
        )
        return bool(claimed.rowcount) and not self._seeded_before_0011(
            round_id, kind, drafts, before
        )

    def _seeded_before_0011(
        self, round_id: int, kind: str, drafts: Sequence[Draft], before: set[tuple[str, str]]
    ) -> bool:
        """Whether a round seeded once for all kinds (``drafts_seeded``, before migration
        0011) seeded this kind: one of its drafts is one only this kind's findings make.

        Decided here, against the drafts the same code generates, rather than backfilled
        in SQL at migration: a seeded draft's text is built by ``seed_drafts`` (severity
        title, explanation, rule, marker text withheld, truncation), and matching that in
        SQL would copy the builder and drift from it. A finding both kinds report proves
        nothing (either could have seeded it); a kind with no draft of its own left (never
        seeded, or the author deleted or edited all of them) is seeded now, and the drafts
        still there are not repeated."""
        row = self._conn.execute(
            "SELECT drafts_seeded FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if row is None or not row[0]:
            return False
        others = {
            (d.path, d.body)
            for other in FINDING_KINDS
            if other != kind
            for d in artifact_drafts(self.artifact(round_id, other))
        }
        own = {(d.path, d.body) for d in drafts} - others
        return bool(own & before)

    def _add_seeds(self, round_id: int, drafts: Sequence[Draft]) -> list[Draft]:
        """Insert claimed seeds after the round's drafts, leaving out any repeating one
        (same file and text), as many as fit under DRAFTS_MAX; count the rest."""
        have = self._draft_keys(round_id)
        count = cast(
            int,
            self._conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE round_id = ?", (round_id,)
            ).fetchone()[0],
        )
        fresh: list[Draft] = []
        for draft in drafts:
            if (draft.path, draft.body) not in have:
                have.add((draft.path, draft.body))
                fresh.append(draft)
        # Each kind is capped on its own by the domain; together they must still leave
        # the author a set they can save back.
        added = fresh[: max(0, DRAFTS_MAX - count)]
        self._insert_drafts(round_id, added)
        self._count_seeds_dropped(round_id, len(fresh) - len(added))
        return added

    def seeds_dropped(self, round_id: int) -> int:
        """How many finding drafts the round had no room for (by seeding or by a save)."""
        row = self._conn.execute(
            "SELECT seeds_dropped FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
        return 0 if row is None else cast(int, row[0])

    def _count_seeds_dropped(self, round_id: int, dropped: int) -> None:
        if dropped > 0:
            self._conn.execute(
                "UPDATE rounds SET seeds_dropped = seeds_dropped + ? WHERE id = ?",
                (dropped, round_id),
            )

    def save_drafts(
        self,
        round_id: int,
        summary: str,
        drafts: Sequence[Draft],
        *,
        seeds: Mapping[str, Sequence[Draft]] | None = None,
    ) -> list[Draft]:
        """Replace a round's summary and drafts (the author's edit) in one transaction.
        ``seeds``: each stored kind of finding's drafts; a kind not seeded yet is claimed
        and its drafts added after the edit (as ``seed_drafts`` does, capped against the
        edit, not the drafts it replaces). Returns the drafts so added. The edit itself
        doesn't count as seeding: findings arriving later still add their drafts. A carried
        draft whose anchor the edit left as it was (same id, file, lines, sides and flag)
        stays pending re-anchoring; any other is the author's choice and is kept as given.

        Raises:
            RoundFrozen: a newer round replaced it (its drafts are there).
        """
        with transaction(self._conn):
            self._refuse_frozen(round_id)
            before = self._draft_keys(round_id)
            claimed = [
                draft
                for kind, generated in (seeds or {}).items()
                if self._claim_seeds(round_id, kind, generated, before)
                for draft in generated
            ]
            pending = {
                row[0]: row[1:]
                for row in self._conn.execute(
                    "SELECT id, path, line, side, start_line, start_side, outdated, "
                    "carried_head, carried_base, body FROM drafts "
                    "WHERE round_id = ? AND carried_head IS NOT NULL",
                    (round_id,),
                )
            }
            self._conn.execute("UPDATE rounds SET summary = ? WHERE id = ?", (summary, round_id))
            self._conn.execute("DELETE FROM drafts WHERE round_id = ?", (round_id,))
            carried: list[tuple[str | None, str | None]] = []
            for d in drafts:
                anchor = (d.path, d.line, d.side, d.start_line, d.start_side, int(d.outdated))
                was = pending.get(d.id) if d.id is not None else None
                if was is None or tuple(was[:6]) != anchor:
                    # Ids change with every save: a draft sent with an older id is matched
                    # by its anchor and text instead (M8).
                    was = next(
                        (w for w in pending.values() if tuple(w[:6]) == anchor and w[8] == d.body),
                        None,
                    )
                if was is not None:
                    pending = {k: v for k, v in pending.items() if v is not was}
                    carried.append((cast(str, was[6]), cast(str, was[7])))
                else:
                    carried.append((None, None))
            self._insert_drafts(round_id, drafts, carried)
            return self._add_seeds(round_id, claimed)

    def postings(self, round_id: int) -> list[Posting]:
        rows = self._conn.execute(
            f"SELECT {_POSTING_COLUMNS} FROM postings WHERE round_id = ? ORDER BY id",
            (round_id,),
        )
        return [_posting(row) for row in rows]

    def unsettled_rounds_of(self, ref: PrRef) -> list[int]:
        """The PR's rounds with an attempt whose outcome isn't known, oldest first."""
        rows = self._conn.execute(
            "SELECT DISTINCT p.round_id FROM postings p JOIN rounds r ON r.id = p.round_id "
            "WHERE r.repo = ? AND r.number = ? AND p.state IN ('started', 'uncertain') "
            "ORDER BY p.round_id",
            (ref.repo, ref.number),
        )
        return [round_id for (round_id,) in rows]

    def _forget_posting_followups(self, posting_id: int) -> None:
        """Drop every follow-up of the posting's PR (R75): the author's latest review changed,
        or may have, so each head is classified again against it."""
        self._conn.execute(
            "DELETE FROM followups WHERE EXISTS (SELECT 1 FROM rounds r JOIN postings p "
            "ON p.round_id = r.id WHERE p.id = ? AND r.repo = followups.repo "
            "AND r.number = followups.number)",
            (posting_id,),
        )

    def unsettled_posting_rounds(self) -> list[int]:
        """Rounds with an attempt whose outcome isn't known (``started`` or ``uncertain``)."""
        rows = self._conn.execute(
            "SELECT DISTINCT round_id FROM postings "
            "WHERE state IN ('started', 'uncertain') ORDER BY round_id"
        )
        return [round_id for (round_id,) in rows]

    def start_posting(
        self,
        round_id: int,
        event: ReviewEvent,
        key: str,
        digest: str,
        body: str,
        comments: object,
        started_at: datetime,
    ) -> Posting:
        """Record an attempt before GitHub is called; a settled unposted attempt with the same
        key is started again. Returns the attempt as stored (a posted one unchanged).

        Raises:
            sqlite3.IntegrityError: another attempt of the round is still in flight.
            RoundFrozen: a newer round replaced it: nothing is posted from it.
        """
        with transaction(self._conn):
            self._refuse_frozen(round_id)
            # While this attempt's outcome isn't known, no head of the PR is classified (R75).
            self._conn.execute(
                "DELETE FROM followups WHERE EXISTS (SELECT 1 FROM rounds r WHERE r.id = ? "
                "AND r.repo = followups.repo AND r.number = followups.number)",
                (round_id,),
            )
            return self._start_posting(round_id, event, key, digest, body, comments, started_at)

    def _start_posting(
        self,
        round_id: int,
        event: ReviewEvent,
        key: str,
        digest: str,
        body: str,
        comments: object,
        started_at: datetime,
    ) -> Posting:
        self._conn.execute(
            "INSERT INTO postings (round_id, event, key, digest, body, comments, started_at, "
            "state) VALUES (?, ?, ?, ?, ?, ?, ?, 'started') "
            "ON CONFLICT (round_id, key) DO UPDATE SET event = excluded.event, "
            "digest = excluded.digest, body = excluded.body, comments = excluded.comments, "
            "started_at = excluded.started_at, state = 'started', error = NULL "
            "WHERE postings.state IN ('failed', 'not_posted')",
            (round_id, event, key, digest, body, json.dumps(comments), _time(started_at)),
        )
        row = self._conn.execute(
            f"SELECT {_POSTING_COLUMNS} FROM postings WHERE round_id = ? AND key = ?",
            (round_id, key),
        ).fetchone()
        return _posting(row)

    def finish_posting(self, posting_id: int, review_url: str) -> None:
        """The review is on GitHub: keep its URL on the attempt and the round (now done)."""
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE postings SET state = 'posted', review_url = ?, error = NULL WHERE id = ?",
                (review_url, posting_id),
            )
            self._conn.execute(
                "UPDATE rounds SET posted_review_url = ?, done = 1 "
                "WHERE id = (SELECT round_id FROM postings WHERE id = ?)",
                (review_url, posting_id),
            )
            # The author's latest review is now this one: every head of the PR (not only the
            # one this round reviewed) is classified again against it (M5, R75).
            self._forget_posting_followups(posting_id)

    def settle_posting(self, posting_id: int, state: PostingState, error: str) -> None:
        """Record how an unposted attempt ended: ``failed`` (GitHub refused it),
        ``uncertain`` (it may be on GitHub) or ``not_posted`` (reconciled: it isn't)."""
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE postings SET state = ?, error = ? WHERE id = ?",
                (state, error, posting_id),
            )
            self._forget_posting_followups(posting_id)  # R75: what the author posted moved
            if state in ("not_posted", "failed"):
                rounds = self._conn.execute(
                    "SELECT round_id FROM postings WHERE id = ?", (posting_id,)
                ).fetchall()
                for (round_id,) in rounds:
                    self._release_held_drafts(cast(int, round_id))

    def posted_rounds(self, ref: PrRef) -> list[tuple[RoundRecord, Posting]]:
        """The PR's rounds with a review posted, each with that posting, oldest first."""
        columns = ", ".join(f"r.{c.strip()}" for c in _ROUND_COLUMNS.split(","))
        posting = ", ".join(f"p.{c.strip()}" for c in _POSTING_COLUMNS.split(","))
        rows = self._conn.execute(
            f"SELECT {columns}, {posting} FROM rounds r JOIN postings p ON p.round_id = r.id "
            "WHERE r.repo = ? AND r.number = ? AND p.state = 'posted' ORDER BY p.id",
            (ref.repo, ref.number),
        )
        width = len(_ROUND_COLUMNS.split(","))
        return [(_round(row[:width]), _posting(row[width:])) for row in rows]

    def posting_comments(self, posting_id: int) -> list[object]:
        """The inline comments an attempt sent, as stored with it (``DraftComment`` fields)."""
        row = self._conn.execute(
            "SELECT comments FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        loaded = [] if row is None else json.loads(row[0])
        return list(loaded) if isinstance(loaded, list) else []

    def put_followup(self, ref: PrRef, head_sha: str, followup: FollowUp) -> None:
        self._conn.execute(
            f"INSERT INTO followups (repo, number, head_sha, {_FOLLOWUP_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repo, number, head_sha) DO UPDATE SET kind = excluded.kind, "
            "differing_paths = excluded.differing_paths, note = excluded.note, "
            "prev_head = excluded.prev_head, prev_source = excluded.prev_source, "
            "error = excluded.error, prev_merge_base = excluded.prev_merge_base, "
            "prev_round = excluded.prev_round, review_key = excluded.review_key",
            (
                ref.repo,
                ref.number,
                head_sha,
                followup.kind,
                json.dumps(followup.differing_paths),
                followup.note,
                followup.prev_head,
                followup.prev_source,
                followup.error,
                followup.prev_merge_base,
                followup.prev_round_id,
                followup.review_key,
            ),
        )

    def postings_fingerprint(self, ref: PrRef) -> str:
        """The PR's postings in one value (the newest attempt and the count per state, over
        all its rounds): any posting started or settled since changes it (R75)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(p.id), 0), "
            "SUM(p.state = 'started'), SUM(p.state = 'uncertain'), SUM(p.state = 'posted'), "
            "SUM(p.state = 'not_posted'), SUM(p.state = 'failed') "
            "FROM postings p JOIN rounds r ON r.id = p.round_id "
            "WHERE r.repo = ? AND r.number = ?",
            (ref.repo, ref.number),
        ).fetchone()
        return "|".join(str(value or 0) for value in row)

    def put_followup_if(
        self, ref: PrRef, head_sha: str, followup: FollowUp, fingerprint: str, instead: FollowUp
    ) -> bool:
        """Store ``followup`` only if the PR's postings are as ``fingerprint`` says and none
        is unsettled, in one transaction; otherwise store ``instead`` (unchecked). Returns
        whether ``followup`` was stored (R75: check, then act)."""
        with transaction(self._conn):
            moved = self.postings_fingerprint(ref) != fingerprint
            if moved or self.unsettled_rounds_of(ref):
                self.put_followup(ref, head_sha, instead)
                return False
            self.put_followup(ref, head_sha, followup)
        return True

    def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None:
        row = self._conn.execute(
            f"SELECT {_FOLLOWUP_COLUMNS} FROM followups "
            "WHERE repo = ? AND number = ? AND head_sha = ?",
            (ref.repo, ref.number, head_sha),
        ).fetchone()
        return None if row is None else _followup(row)

    def current_followups(self) -> dict[PrRef, FollowUp]:
        """Each queued PR's follow-up for its current head, where it is known."""
        rows = self._conn.execute(
            "SELECT f.repo, f.number, f.kind, f.differing_paths, f.note, f.prev_head, "
            "f.prev_source, f.error, f.prev_merge_base, f.prev_round, f.review_key "
            "FROM followups f "
            "JOIN prs p ON p.repo = f.repo AND p.number = f.number AND p.head_sha = f.head_sha"
        )
        return {PrRef(row[0], row[1]): _followup(row[2:]) for row in rows}

    def set_verdict(self, round_id: int, verdict: str) -> None:
        self._conn.execute("UPDATE rounds SET verdict = ? WHERE id = ?", (verdict, round_id))

    def artifact(self, round_id: int, kind: str) -> object:
        """One artifact of a round (``None`` if there is none), decoding only that one."""
        row = self._conn.execute(
            "SELECT json FROM artifacts WHERE round_id = ? AND kind = ?", (round_id, kind)
        ).fetchone()
        return None if row is None else with_clean_titles(kind, json.loads(row[0]))

    def artifact_kinds(self, round_id: int) -> set[str]:
        """The kinds of artifact a round has, without reading them."""
        rows = self._conn.execute("SELECT kind FROM artifacts WHERE round_id = ?", (round_id,))
        return {kind for (kind,) in rows}

    def artifacts(self, round_id: int) -> dict[str, object]:
        """Every artifact of a round, by kind."""
        rows = self._conn.execute(
            "SELECT kind, json FROM artifacts WHERE round_id = ?", (round_id,)
        )
        return {kind: with_clean_titles(kind, json.loads(data)) for kind, data in rows}

    def put_artifact(self, round_id: int, kind: str, data: object) -> None:
        self._conn.execute(
            "INSERT INTO artifacts (round_id, kind, json) VALUES (?, ?, ?) "
            "ON CONFLICT (round_id, kind) DO UPDATE SET json = excluded.json",
            (round_id, kind, json.dumps(data)),
        )

    # Stack reviews ----------------------------------------------------------------------

    def stack_check(self, repo: str, signature: str) -> str | None:
        """R77 for one set of heads: '' when the top head contains every member head, the
        reason when it doesn't, ``None`` when it wasn't checked yet."""
        row = self._conn.execute(
            "SELECT reason FROM stack_checks WHERE repo = ? AND signature = ?",
            (repo, signature),
        ).fetchone()
        return None if row is None else cast(str, row[0])

    def put_stack_check(self, repo: str, signature: str, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO stack_checks (repo, signature, reason) VALUES (?, ?, ?) "
            "ON CONFLICT (repo, signature) DO UPDATE SET reason = excluded.reason",
            (repo, signature, reason),
        )

    def restacks(self) -> dict[tuple[str, str], str]:
        """Every checked set of heads that needs a restack, with its reason (R77)."""
        rows = self._conn.execute(
            "SELECT repo, signature, reason FROM stack_checks WHERE reason != ''"
        )
        return {(repo, signature): reason for repo, signature, reason in rows}

    def stack_review(self, repo: str, signature: str) -> int | None:
        """The stack review of ``repo``'s PRs at the heads ``signature`` names, if any."""
        row = self._conn.execute(
            "SELECT id FROM stack_reviews WHERE repo = ? AND signature = ?", (repo, signature)
        ).fetchone()
        return None if row is None else int(row[0])

    def start_stack_review(self, repo: str, signature: str, started_at: datetime) -> int:
        """The stack review for ``signature``, created if it doesn't exist yet."""
        # The no-op update makes RETURNING answer for an existing row too.
        row = self._conn.execute(
            "INSERT INTO stack_reviews (repo, signature, started_at) VALUES (?, ?, ?) "
            "ON CONFLICT (repo, signature) DO UPDATE SET repo = excluded.repo RETURNING id",
            (repo, signature, _time(started_at)),
        ).fetchone()
        return int(row[0])

    def stack_artifacts(self, stack_id: int) -> dict[str, object]:
        rows = self._conn.execute(
            "SELECT kind, json FROM stack_artifacts WHERE stack_id = ?", (stack_id,)
        )
        return {kind: with_clean_titles(kind, json.loads(data)) for kind, data in rows}

    def stack_artifact(self, stack_id: int, kind: str) -> object:
        row = self._conn.execute(
            "SELECT json FROM stack_artifacts WHERE stack_id = ? AND kind = ?", (stack_id, kind)
        ).fetchone()
        return None if row is None else with_clean_titles(kind, json.loads(row[0]))

    def stack_jobs(self) -> dict[int, object]:
        """Every stack review's ``jobs`` record, by stack review."""
        rows = self._conn.execute("SELECT stack_id, json FROM stack_artifacts WHERE kind = 'jobs'")
        return {int(stack_id): json.loads(data) for stack_id, data in rows}

    def put_stack_artifact(self, stack_id: int, kind: str, data: object) -> None:
        self._conn.execute(
            "INSERT INTO stack_artifacts (stack_id, kind, json) VALUES (?, ?, ?) "
            "ON CONFLICT (stack_id, kind) DO UPDATE SET json = excluded.json",
            (stack_id, kind, json.dumps(data)),
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
            "SELECT r.repo, r.head_sha, r.base_sha FROM rounds r WHERE r.finished_at IS NULL "
            "UNION ALL "
            "SELECT r.repo, r.head_sha, r.base_sha FROM rounds r "
            "JOIN prs p ON p.repo = r.repo AND p.number = r.number "
            "WHERE p.dismissed = 0 AND r.done = 0 AND r.id IN ("
            "SELECT MAX(id) FROM rounds WHERE finished_at IS NOT NULL GROUP BY repo, number)"
        )
        return {(repo, sha) for repo, head, base in rows for sha in (head, base) if sha}
