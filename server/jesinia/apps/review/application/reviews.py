# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a PR's review, marking it done, and Settings → Review.

Everything here reads ``review.sqlite`` only, never GitHub (spec §2, "Always cached"): an
opened PR renders from its cached row and latest round at once, with the queue's sync state
beside it, and a cached review whose head the PR has moved past is marked stale.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

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
    queue_row,
)
from jesinia.services.github.domain.pr import PrFile

# A runner name names an agent CLI the agents service knows; nothing path-like.
_RUNNER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
FINDING_SOURCES: Final = ("correctness", "findings")


class ReviewReadStore(Protocol):
    """The ``review.sqlite`` reads (and the few setting writes) this needs."""

    def pr(self, ref: PrRef) -> StoredPr | None: ...

    def viewer(self) -> str | None: ...

    def latest_rounds(self) -> dict[PrRef, RoundSummary]: ...

    def suggested_repos(self) -> list[str]: ...

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
    """Settings → Review: the pre-review default, the agent runner, and per-repo choices."""

    prereview: PreReviewMode
    runner: str
    repos: tuple[RepoConfig, ...]


class ReviewReads:
    """Cached reads of reviews, and the review settings.

    Args:
        store: ``review.sqlite``.
        status: the queue's sync state (``QueueService.status``), never awaited.
    """

    def __init__(self, store: ReviewReadStore, status: Callable[[], SyncStatus]) -> None:
        self._store = store
        self._status = status

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
        row = queue_row(stored, latest, self._store.viewer(), suggested)
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

    def settings(self) -> ReviewSettings:
        configs = self._store.repo_configs()
        return ReviewSettings(
            self._store.prereview_default(),
            self._store.job_runner(),
            tuple(configs[repo] for repo in sorted(configs)),
        )

    def update_settings(
        self, prereview: PreReviewMode, runner: str, repos: Sequence[RepoConfig]
    ) -> ReviewSettings:
        """Save Settings → Review; repos not listed keep their settings.

        Raises:
            InvalidSettingError: a runner name that isn't one.
        """
        if not _RUNNER.fullmatch(runner):
            raise InvalidSettingError(f"not an agent runner name: {runner!r}")
        self._store.set_prereview_default(prereview)
        self._store.set_job_runner(runner)
        for config in repos:
            self._store.upsert_repo(config)
        return self.settings()
