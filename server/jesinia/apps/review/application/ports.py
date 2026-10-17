# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the review app needs from the outside world; implemented in ``infrastructure``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from jesinia.apps.review.domain.gates import Depth
from jesinia.apps.review.domain.queue import (
    PreReviewMode,
    PreReviewState,
    PrRef,
    PrSnapshot,
    RepoConfig,
    RoundStatus,
    RoundSummary,
    StoredPr,
)
from jesinia.apps.review.domain.repo_access import CappedOutput, RegexDialect
from jesinia.services.github.domain.pr import CheckState, PrFile, PullRequest, ReviewSummary


class Workspace(Protocol):
    """Local clones of watched repos: one bare mirror per repo plus one detached worktree
    per reviewed head. Read-only towards GitHub: it fetches and never pushes.

    Every method validates ``repo`` (``owner/name``) and SHAs (full 40-hex) before running
    git and raises ``jesinia.apps.review.domain.workspace.WorkspaceError`` subclasses.
    """

    async def ensure_mirror(self, repo: str) -> None:
        """Create the repo's bare mirror if missing and point it at the remote."""

    async def fetch(self, repo: str, refs: list[str]) -> None:
        """Fetch full refs (``refs/pull/7/head``) into the same names, or bare SHAs."""

    async def worktree(self, repo: str, sha: str) -> Path:
        """A detached checkout of ``sha``; calling again returns the same path."""

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        """The unified diff from ``base_sha`` to ``head_sha``."""

    async def merge_base(self, repo: str, a: str, b: str) -> str:
        """The best common ancestor of two commits."""

    async def patch_ids(self, repo: str, base_sha: str, head_sha: str) -> dict[str, str]:
        """Each changed path mapped to the ``git patch-id --stable`` of its own diff, so a
        pure rebase compares equal file by file and a conflict resolution does not."""

    async def prune(self, keep: set[tuple[str, str]]) -> int:
        """Remove every worktree whose ``(repo, sha)`` is not in ``keep``; return how many.
        A repo whose mirror is broken is logged and skipped; the others are still pruned."""

    def tree_key(self, path: Path) -> tuple[str, str] | None:
        """The canonical ``(repo, sha)`` of a worktree path, or ``None`` if ``path`` is not
        one of this workspace's worktrees."""


class PrunableWorkspace(Protocol):
    """The part of ``Workspace`` the prune job uses."""

    async def prune(self, keep: set[tuple[str, str]]) -> int: ...

    def tree_key(self, path: Path) -> tuple[str, str] | None: ...


class TreeGit(Protocol):
    """Read-only git inside a review worktree, for the repo tools.

    Runs with the workspace's hardened environment and without the system config (local
    reads need no credentials). Raises ``WorkspaceError`` subclasses, including for a
    ``tree`` outside the workspace.
    """

    async def read_in_tree(
        self,
        tree: Path,
        args: Sequence[str],
        *,
        output_limit: int,
        glob_pathspecs: bool = False,
        accept_exit_codes: frozenset[int] = frozenset({0}),
        without_worktree: bool = False,
        timeout: float | None = None,
    ) -> CappedOutput:
        """Run ``git <args>`` in ``tree``; stdout is cut (and the process killed) past
        ``output_limit`` bytes. ``glob_pathspecs`` lets ``:(glob)`` pathspecs work, which
        are otherwise literal. An exit code outside ``accept_exit_codes`` raises.
        ``without_worktree`` runs against the tree's repository only, so no file in the
        checkout (``.mailmap``) is read; revisions must then be full commit ids.
        ``timeout`` overrides the workspace's default time limit, in seconds."""

    async def regex_dialect(self) -> RegexDialect:
        """Which ERE extensions the installed git's regex library supports (probed once)."""


class PullRequests(Protocol):
    """The GitHub reads the queue needs (``services.github`` ``PrClient``).

    Every method may raise ``GitHubError`` subclasses: ``GitHubRateLimitedError`` when
    GitHub is rate limiting, ``GitHubUnavailable`` without a token.
    """

    async def viewer(self) -> str: ...

    async def list_review_inbox(self, repos: list[str]) -> list[PullRequest]: ...

    async def get(self, repo: str, number: int) -> PullRequest: ...

    async def checks(self, repo: str, sha: str) -> CheckState: ...

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]: ...

    async def files(self, repo: str, number: int) -> list[PrFile]: ...


class QueueStore(Protocol):
    """The queue's rows, rounds and settings in ``review.sqlite``. Repos are canonical."""

    def watched_repos(self) -> list[str]: ...

    def repo_configs(self) -> dict[str, RepoConfig]: ...

    def upsert_repo(self, config: RepoConfig) -> None: ...

    def prereview_default(self) -> PreReviewMode: ...

    def set_prereview_default(self, mode: PreReviewMode) -> None: ...

    def viewer(self) -> str | None: ...

    def set_viewer(self, login: str) -> None: ...

    def sync_status(self) -> tuple[datetime | None, str | None]:
        """When the last good sync finished, and why the latest one failed (if it did)."""

    def record_sync(self, synced_at: datetime | None, error: str | None) -> None:
        """Store a sync's outcome; ``synced_at=None`` keeps the last good sync's time."""

    def pr(self, ref: PrRef) -> StoredPr | None: ...

    def prs(self, include_dismissed: bool = False) -> list[StoredPr]: ...

    def apply_sync(
        self,
        fresh: Sequence[PrSnapshot],
        failed: Sequence[tuple[PrSnapshot, str]],
        removed: Sequence[PrRef],
        now: datetime,
    ) -> None:
        """In one transaction: upsert ``fresh`` rows (a new head restarts the debounce),
        flag ``failed`` ones (only the error and its time change on a cached row; an
        uncached one is inserted from what is known), and delete ``removed``."""

    def set_loaded(self, ref: PrRef) -> StoredPr | None:
        """Mark loaded (and no longer dismissed) and return the row; None if not cached."""

    def add_loaded(self, snapshot: PrSnapshot, now: datetime) -> StoredPr:
        """Cache a PR fetched for loading, marked loaded, and return its row."""

    def set_dismissed(self, ref: PrRef) -> bool:
        """Mark dismissed (and no longer loaded); False if the PR isn't cached."""

    def set_state(self, ref: PrRef, head_sha: str, state: PreReviewState, reason: str) -> None:
        """Record the pre-review state, only while the PR's head is still ``head_sha``."""

    def start_round(
        self, ref: PrRef, head_sha: str, base_sha: str, depth: Depth, started_at: datetime
    ) -> int: ...

    def finish_round(
        self, round_id: int, finished_at: datetime, error: str | None, verdict: str | None = None
    ) -> None: ...

    def mark_round_done(self, round_id: int) -> None: ...

    def fail_unfinished_rounds(self, at: datetime, cause: str) -> int: ...

    def rounds_for_heads(self) -> dict[tuple[PrRef, str], RoundStatus]:
        """The latest round on each (PR, head)."""

    def latest_rounds(self) -> dict[PrRef, RoundSummary]:
        """Each PR's latest successfully finished round."""

    def put_artifact(self, round_id: int, kind: str, data: object) -> None: ...

    def pr_files(self, ref: PrRef, head_sha: str) -> list[PrFile] | None: ...

    def put_pr_files(self, ref: PrRef, head_sha: str, files: Sequence[PrFile]) -> None: ...

    def keep_shas(self) -> set[tuple[str, str]]:
        """(repo, sha) of the head and base of every queued PR and of rounds still in review."""
