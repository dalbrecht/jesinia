# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a PR's review, marking it done, a repo's metrics over time, and Settings → Review.

Everything here reads ``review.sqlite`` only, never GitHub (spec §2, "Always cached"): an
opened PR renders from its cached row and latest round at once, with the queue's sync state
beside it, and a cached review whose head the PR has moved past is marked stale.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Final, Literal, Protocol

from jesinia.apps.review.domain.followup import FollowUp
from jesinia.apps.review.domain.metrics import Point, point_of
from jesinia.apps.review.domain.posting import Draft, NoReviewRound, RoundChanged
from jesinia.apps.review.domain.queue import (
    InvalidSettingError,
    PreReviewMode,
    PrNotInQueueError,
    PrRef,
    QueueRow,
    RepoConfig,
    RoundRecord,
    RoundSummary,
    StoredPr,
    SyncStatus,
    nothing_to_walk_from_json,
    parse_folder_setting,
    queue_row,
)
from jesinia.apps.review.domain.stacks import stack_numbers
from jesinia.services.github.domain.pr import PrFile

# A runner name names an agent CLI the agents service knows; nothing path-like.
_RUNNER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
FINDING_SOURCES: Final = ("correctness", "findings")
# The trend shows a repo's latest scored rounds: enough to see a drift, bounded to read.
HISTORY_LIMIT: Final = 200


class Keep(Enum):
    """A setting left out of an update: it keeps its value."""

    KEEP = "keep"


KEEP: Final = Keep.KEEP


class ReviewReadStore(Protocol):
    """The ``review.sqlite`` reads (and the few setting writes) this needs."""

    def pr(self, ref: PrRef) -> StoredPr | None: ...

    def prs(self, include_dismissed: bool = False) -> list[StoredPr]: ...

    def viewer(self) -> str | None: ...

    def latest_rounds(self) -> dict[PrRef, RoundSummary]: ...

    def current_followups(self) -> dict[PrRef, FollowUp]: ...

    def suggested_repos(self) -> list[str]: ...

    def restacks(self) -> dict[tuple[str, str], str]: ...

    def latest_round(self, ref: PrRef) -> RoundRecord | None: ...

    def artifacts(self, round_id: int) -> dict[str, object]: ...

    def pr_files(self, ref: PrRef, head_sha: str) -> list[PrFile] | None: ...

    def drafts(self, round_id: int) -> list[Draft]: ...

    def mark_round_done(self, round_id: int) -> None: ...

    def prereview_default(self) -> PreReviewMode: ...

    def set_prereview_default(self, mode: PreReviewMode) -> None: ...

    def job_runner(self) -> str: ...

    def set_job_runner(self, runner: str) -> None: ...

    def repo_configs(self) -> dict[str, RepoConfig]: ...

    def upsert_repo(self, config: RepoConfig) -> None: ...

    def standards_dir(self) -> Path | None: ...

    def set_standards_dir(self, path: Path | None) -> None: ...

    def standards_user_dir(self) -> Path | None: ...

    def set_standards_user_dir(self, path: Path | None) -> None: ...

    def metric_rounds(
        self, repo: str, limit: int
    ) -> list[tuple[int, int, str, datetime, object]]: ...


@dataclass(frozen=True)
class PrView:
    """An opened PR as cached: its queue row, its latest round (``None`` before any) with
    every artifact, the queue's sync state, and GitHub's cached files (with their patches)
    for the round's head: ``None`` before any round or while that head's list isn't cached,
    never another head's."""

    row: QueueRow
    round: RoundRecord | None
    artifacts: Mapping[str, object]
    sync: SyncStatus
    files: Sequence[PrFile] | None = None

    @property
    def stale(self) -> bool:
        """The PR has moved past the head the cached review covers."""
        return self.round is not None and self.round.head_sha != self.row.head_sha

    @property
    def nothing_to_walk(self) -> bool:
        return nothing_to_walk_from_json(self.artifacts.get("coverage"))


@dataclass(frozen=True)
class FindingsView:
    """The latest round's findings (agent output, both passes) and the author's drafts."""

    round_id: int | None
    findings: tuple[Mapping[str, object], ...]
    drafts: tuple[Draft, ...]


@dataclass(frozen=True)
class ReviewSettings:
    """Settings → Review: the pre-review default, the agent runner, the standards folders
    (the coding-standards checkout, the base layer; the author's own documents, the user
    layer), and per-repo choices."""

    prereview: PreReviewMode
    runner: str
    repos: tuple[RepoConfig, ...]
    standards_dir: Path | None = None
    standards_user_dir: Path | None = None


class ReviewReads:
    """Cached reads of reviews, and the review settings.

    Args:
        store: ``review.sqlite``.
        status: the queue's sync state (``QueueService.status``), never awaited.
    """

    def __init__(
        self,
        store: ReviewReadStore,
        status: Callable[[], SyncStatus],
        home: Callable[[], Path] = Path.home,
    ) -> None:
        self._store = store
        self._status = status
        self._home = home

    def _stored(self, ref: PrRef) -> StoredPr:
        stored = self._store.pr(ref)
        if stored is None:
            raise PrNotInQueueError(f"{ref} is not in the review queue; load it first")
        return stored

    def pr(self, ref: PrRef) -> PrView:
        """The PR's cached view.

        Raises:
            PrNotInQueueError: it isn't cached.
        """
        stored = self._stored(ref)
        latest = self._store.latest_rounds().get(ref)
        suggested = ref.repo in self._store.suggested_repos()
        followup = self._store.current_followups().get(ref)
        everyone = [s.pr for s in self._store.prs(include_dismissed=True)]
        stack = stack_numbers(everyone, self._store.restacks()).get(ref, ())
        row = queue_row(stored, latest, self._store.viewer(), suggested, followup, stack)
        found = self._store.latest_round(ref)
        artifacts = {} if found is None else self._store.artifacts(found.id)
        files = None if found is None else self._store.pr_files(ref, found.head_sha)
        return PrView(row, found, artifacts, self._status(), files)

    def findings(self, ref: PrRef) -> FindingsView:
        """The latest round's findings and drafts (both empty before any round).

        Raises:
            PrNotInQueueError: it isn't cached.
        """
        self._stored(ref)
        found = self._store.latest_round(ref)
        if found is None:
            return FindingsView(None, (), ())
        artifacts = self._store.artifacts(found.id)
        findings: list[Mapping[str, object]] = []
        for source in FINDING_SOURCES:
            data = artifacts.get(source)
            items = data.get("findings") if isinstance(data, Mapping) else None
            for item in items if isinstance(items, list) else []:
                if isinstance(item, Mapping):
                    findings.append({**item, "source": source})
        return FindingsView(found.id, tuple(findings), tuple(self._store.drafts(found.id)))

    def done(self, ref: PrRef, round_id: int | None = None) -> RoundRecord:
        """Mark the PR's latest round done (the author finished it without posting).
        ``round_id``: the round the author was shown; a newer one refuses it (never moved).

        Raises:
            NoReviewRound: the PR has no round.
            RoundChanged: ``round_id`` isn't the latest round.
            RoundFrozen: a newer round took it over (a ``RoundChanged``).
        """
        found = self._store.latest_round(ref)
        if found is None:
            raise NoReviewRound(f"{ref} has no review round to finish")
        if round_id is not None and round_id != found.id:
            raise RoundChanged(
                f"a new review round ({found.id}) started since round {round_id} was shown; "
                "it wasn't marked done"
            )
        self._store.mark_round_done(found.id)
        return replace(found, done=True)

    def metrics_history(self, repo: str) -> list[Point]:
        """``repo``'s scored rounds, oldest first: its latest ``HISTORY_LIMIT`` (spec §5.2,
        "scores are kept per repo over time"). A round's scores are the set it was scored on
        then; nothing is recomputed.

        Raises:
            InvalidPrRefError: ``repo`` isn't ``owner/name``.
        """
        rows = self._store.metric_rounds(RepoConfig(repo).repo, HISTORY_LIMIT)
        points = (point_of(*row) for row in rows)
        return [point for point in points if point is not None]

    def settings(self) -> ReviewSettings:
        configs = self._store.repo_configs()
        return ReviewSettings(
            self._store.prereview_default(),
            self._store.job_runner(),
            tuple(configs[repo] for repo in sorted(configs)),
            self._store.standards_dir(),
            self._store.standards_user_dir(),
        )

    def repo_config(self, repo: str) -> RepoConfig | None:
        """``repo``'s saved settings, if any.

        Raises:
            InvalidPrRefError: ``repo`` isn't ``owner/name``.
        """
        return self._store.repo_configs().get(RepoConfig(repo).repo)

    def update_settings(
        self,
        prereview: PreReviewMode | Literal[Keep.KEEP],
        runner: str | Literal[Keep.KEEP],
        repos: Sequence[RepoConfig],
        *,
        standards_dir: str | None | Literal[Keep.KEEP] = KEEP,
        standards_user_dir: str | None | Literal[Keep.KEEP] = KEEP,
    ) -> ReviewSettings:
        """Save Settings → Review; repos not listed, and the default, runner and folders
        left as ``KEEP``, keep their settings. Everything is checked before anything is saved.

        Raises:
            InvalidSettingError: a runner name that isn't one, or a folder that isn't an
                absolute path (or ``~/…``).
        """
        if runner is not KEEP and not _RUNNER.fullmatch(runner):
            raise InvalidSettingError(f"not an agent runner name: {runner!r}")
        folders: list[tuple[Callable[[Path | None], None], Path | None]] = []
        if standards_dir is not KEEP:
            path = parse_folder_setting(standards_dir, self._home())
            folders.append((self._store.set_standards_dir, path))
        if standards_user_dir is not KEEP:
            path = parse_folder_setting(standards_user_dir, self._home())
            folders.append((self._store.set_standards_user_dir, path))
        if prereview is not KEEP:
            self._store.set_prereview_default(prereview)
        if runner is not KEEP:
            self._store.set_job_runner(runner)
        for setter, folder in folders:
            setter(folder)
        for config in repos:
            self._store.upsert_repo(config)
        return self.settings()
