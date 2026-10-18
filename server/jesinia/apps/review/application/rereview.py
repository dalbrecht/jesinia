# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental re-reviews (spec §4.2): what a re-review of a PR's new head builds on.

- ``changed_hunks``: the round's hunks whose content isn't in the previous review's diff
  (merge base to reviewed head, rebuilt in the local mirror), following renames between the
  two heads. Whatever can't be compared counts as new (fail closed).
- ``previous_comments``: the author's previous comments. From the stored round, its review as
  posted (the body and inline comments Jesinia sent); with no stored round (a review posted
  on GitHub by hand, or a round since pruned), the author's review threads on GitHub.
- ``new_changes``: the text of the new hunks, for the ``rereview`` job, capped.

Every read here is input to an agent job, never a write: nothing here calls GitHub's write
API. Failures are raised typed (``RereviewInputError``, the cause chained), for the pipeline
to record on the re-review job alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Protocol

from jesinia.apps.review.domain.coverage import (
    DiffParseError,
    HunkRef,
    ParsedDiff,
    parse_unified_diff,
)
from jesinia.apps.review.domain.followup import FollowUp
from jesinia.apps.review.domain.posting import Posting
from jesinia.apps.review.domain.queue import PrRef, RoundRecord
from jesinia.apps.review.domain.rereview import (
    PreviousReview,
    RereviewInputError,
    changed_hunks,
    comments_from_posting,
    comments_from_threads,
    new_changes_text,
)
from jesinia.apps.review.domain.workspace import DiffTooLargeError, WorkspaceError
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import GitHubError
from jesinia.services.github.domain.pr import ReviewSummary, ReviewThread

log = logging.getLogger(__name__)

_GITHUB_ERRORS = (GitHubError, GitHubPayloadError)


class RereviewGitHub(Protocol):
    """GitHub reads (``PrClient``); each may raise ``GitHubError``."""

    async def threads(self, repo: str, number: int) -> list[ReviewThread]: ...

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]: ...

    async def viewer(self) -> str: ...


class RereviewWorkspace(Protocol):
    """The local mirror (``GitWorkspace``); each may raise ``WorkspaceError``."""

    async def ensure_mirror(self, repo: str) -> None: ...

    async def fetch(self, repo: str, refs: list[str]) -> None: ...

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str: ...

    async def renames(self, repo: str, old_sha: str, new_sha: str) -> dict[str, str]: ...


class RereviewStore(Protocol):
    """``review.sqlite``."""

    def viewer(self) -> str | None: ...

    def posted_rounds(self, ref: PrRef) -> list[tuple[RoundRecord, Posting]]: ...

    def posting_comments(self, posting_id: int) -> list[object]: ...


def _every_hunk(diff: ParsedDiff) -> frozenset[HunkRef]:
    return changed_hunks(None, diff, {})


class Rereviews:
    """What re-reviews build on.

    Args:
        github: review threads, reviews and the viewer (``PrClient``).
        workspace: the local mirror (``GitWorkspace``).
        store: ``review.sqlite``.
    """

    def __init__(
        self, *, github: RereviewGitHub, workspace: RereviewWorkspace, store: RereviewStore
    ) -> None:
        self._github = github
        self._workspace = workspace
        self._store = store

    async def changed_hunks(
        self, ref: PrRef, followup: FollowUp, head_sha: str, diff: ParsedDiff
    ) -> frozenset[HunkRef]:
        """The hunks of ``diff`` (the round's merge base to ``head_sha``) new since the
        previous review. Every hunk when the previous diff can't be rebuilt (no merge base
        known, too large, unreadable) or ``diff`` is incomplete.

        Raises:
            WorkspaceError: the mirror couldn't diff the previous review or list renames;
                the caller counts every hunk as new and says why.
        """
        prev_head, prev_base = followup.prev_head, followup.prev_merge_base
        if prev_head is None or prev_base is None or not diff.complete:
            return _every_hunk(diff)
        repo = ref.repo
        await self._workspace.ensure_mirror(repo)
        await self._fetch(repo, [prev_base, prev_head, head_sha])
        try:
            text = await self._workspace.diff(repo, prev_base, prev_head)
            previous = await asyncio.to_thread(parse_unified_diff, text)
        except (DiffTooLargeError, DiffParseError) as exc:
            log.info("review.rereview %s: previous diff unread, every hunk is new: %s", ref, exc)
            return _every_hunk(diff)
        renames = await self._workspace.renames(repo, prev_head, head_sha)
        return changed_hunks(previous, diff, renames)

    async def _fetch(self, repo: str, shas: list[str]) -> None:
        """Fetch commits that may already be in the mirror; a failure is left to the reads
        that need them, which say what is missing."""
        try:
            await self._workspace.fetch(repo, sorted(set(shas)))
        except WorkspaceError as exc:
            log.info("review.rereview %s: fetch of %d commits failed: %s", repo, len(shas), exc)

    async def previous_comments(self, ref: PrRef, followup: FollowUp) -> PreviousReview:
        """The author's previous comments: the stored round's review as posted, or, with no
        stored round, their review threads (and review body) on GitHub.

        Raises:
            RereviewInputError: GitHub couldn't be read (the cause is chained).
        """
        head = followup.prev_head or ""
        if followup.prev_source == "round" and followup.prev_round_id is not None:
            for rnd, posting in reversed(self._store.posted_rounds(ref)):
                if rnd.id == followup.prev_round_id:
                    comments = comments_from_posting(
                        posting.body, self._store.posting_comments(posting.id)
                    )
                    return PreviousReview("round", rnd.head_sha, tuple(comments), rnd.id)
            log.info(
                "review.rereview %s: round %d is gone, reading GitHub", ref, followup.prev_round_id
            )
        try:
            viewer = self._store.viewer() or await self._github.viewer()
            threads = await self._github.threads(ref.repo, ref.number)
            reviews = await self._github.reviews(ref.repo, ref.number)
        except _GITHUB_ERRORS as exc:
            raise RereviewInputError(
                f"your previous review's comments couldn't be read from GitHub: {exc}"
            ) from exc
        mine = [
            review
            for review in reviews
            if review.author.lower() == viewer.lower() and review.commit_sha == head
        ]
        body = mine[-1].body if mine else ""
        return PreviousReview("github", head, tuple(comments_from_threads(threads, viewer, body)))

    async def new_changes(
        self, ref: PrRef, merge_base: str, head_sha: str, refs: Collection[HunkRef]
    ) -> tuple[str, int]:
        """The text of the hunks ``refs`` of the round's diff, capped; and how many hunks
        were left out for the cap.

        Raises:
            RereviewInputError: the diff couldn't be read again (the cause is chained).
        """
        try:
            text = await self._workspace.diff(ref.repo, merge_base, head_sha)
            diff = await asyncio.to_thread(parse_unified_diff, text, keep_text=True)
        except (WorkspaceError, DiffParseError) as exc:
            raise RereviewInputError(f"the new changes couldn't be read: {exc}") from exc
        return new_changes_text(diff, refs)
