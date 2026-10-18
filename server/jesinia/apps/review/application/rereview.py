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
from collections.abc import Collection, Mapping
from dataclasses import replace
from typing import Protocol

from jesinia.apps.review.application.followup import line_maps
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
    match_hunks,
    new_changes_text,
    place,
    previous_findings,
    previous_steps,
)
from jesinia.apps.review.domain.workspace import DiffTooLargeError, WorkspaceError
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import GitHubError
from jesinia.services.github.domain.pr import ReviewSide, ReviewSummary, ReviewThread

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

    async def show_file(self, repo: str, sha: str, path: str, *, limit: int) -> bytes | None: ...


class RereviewStore(Protocol):
    """``review.sqlite``."""

    def viewer(self) -> str | None: ...

    def posted_rounds(self, ref: PrRef) -> list[tuple[RoundRecord, Posting]]: ...

    def posting_comments(self, posting_id: int) -> list[object]: ...

    def artifact(self, round_id: int, kind: str) -> object: ...


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
        if followup.prev_head is None or followup.prev_merge_base is None or not diff.complete:
            return _every_hunk(diff)
        await self._workspace.ensure_mirror(ref.repo)
        await self._fetch(ref.repo, [head_sha])
        previous = await self._previous_diff(ref.repo, followup)
        if previous is None:
            return _every_hunk(diff)
        renames = await self._workspace.renames(ref.repo, followup.prev_head, head_sha)
        return changed_hunks(previous, diff, renames)

    async def _previous_diff(self, repo: str, followup: FollowUp) -> ParsedDiff | None:
        """The previous review's diff (its merge base to its head), or ``None`` when it can't
        be rebuilt (no merge base known, too large, unparsable).

        Raises:
            WorkspaceError: git failed otherwise.
        """
        prev_head, prev_base = followup.prev_head, followup.prev_merge_base
        if prev_head is None or prev_base is None:
            return None
        await self._fetch(repo, [prev_base, prev_head])
        try:
            text = await self._workspace.diff(repo, prev_base, prev_head)
            return await asyncio.to_thread(parse_unified_diff, text)
        except (DiffTooLargeError, DiffParseError) as exc:
            log.info("review.rereview %s: previous diff unread: %s", repo, exc)
            return None

    async def previous_review(
        self, ref: PrRef, followup: FollowUp, head_sha: str, merge_base: str, diff: ParsedDiff
    ) -> PreviousReview:
        """The previous review on the head under review (spec §4.2 "Previous review in
        context"): its comments, and for a stored round its walkthrough steps and findings,
        each placed by this head's paths, lines and hunks (renames followed, I2). What can't
        be placed (its text changed, or git can't say) is outdated, never misplaced.

        Raises:
            RereviewInputError: the comments couldn't be read from GitHub.
        """
        previous = await self.previous_comments(ref, followup)
        walkthrough: object = None
        findings: list[object] = []
        if previous.round_id is not None:
            walkthrough = self._store.artifact(previous.round_id, "walkthrough")
            kinds = ("correctness", "findings")
            findings = [self._store.artifact(previous.round_id, kind) for kind in kinds]
        keys = {(c.path, c.side or "RIGHT") for c in previous.comments if c.path is not None}
        keys |= {
            (str(item["path"]), "RIGHT")
            for artifact in findings
            if isinstance(artifact, Mapping) and isinstance(artifact.get("findings"), list)
            for item in artifact["findings"]
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        }
        right = None if followup.prev_head is None else (followup.prev_head, head_sha)
        left = None if followup.prev_merge_base is None else (followup.prev_merge_base, merge_base)
        moved: dict[tuple[str, ReviewSide], str] = {}
        matches: dict[HunkRef, HunkRef] = {}
        try:
            await self._workspace.ensure_mirror(ref.repo)
            await self._fetch(ref.repo, [sha for pair in (right, left) if pair for sha in pair])
            maps = await line_maps(
                self._workspace, ref.repo, {"RIGHT": right, "LEFT": left}, keys, moved
            )
            old_diff = await self._previous_diff(ref.repo, followup) if walkthrough else None
            if old_diff is not None and right is not None:
                renames = await self._workspace.renames(ref.repo, right[0], head_sha)
                matched = match_hunks(old_diff, diff, renames)
                matches = {old: new for new, old in matched.items()}
        except WorkspaceError as exc:  # secondary: everything stays outdated, with the cause
            log.warning("review.rereview %s: previous review not placed: %s", ref, exc)
            maps, moved = {}, {}
        return replace(
            previous,
            comments=tuple(place(c, maps, moved) for c in previous.comments),
            steps=previous_steps(walkthrough, matches),
            findings=previous_findings(findings, maps, moved),
        )

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
