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
from dataclasses import dataclass, replace
from typing import Protocol

from jesinia.apps.review.application.followup import line_maps
from jesinia.apps.review.domain.coverage import (
    DiffParseError,
    HunkRef,
    ParsedDiff,
    parse_unified_diff,
)
from jesinia.apps.review.domain.followup import FollowUp, LineMaps
from jesinia.apps.review.domain.posting import Posting
from jesinia.apps.review.domain.queue import PrRef, RoundRecord
from jesinia.apps.review.domain.rereview import (
    PreviousComment,
    PreviousFinding,
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

    async def merge_base(self, repo: str, a: str, b: str) -> str: ...

    async def show_file(self, repo: str, sha: str, path: str, *, limit: int) -> bytes | None: ...


class RereviewStore(Protocol):
    """``review.sqlite``."""

    def viewer(self) -> str | None: ...

    def posted_rounds(self, ref: PrRef) -> list[tuple[RoundRecord, Posting]]: ...

    def posting_comments(self, posting_id: int) -> list[object]: ...

    def artifact(self, round_id: int, kind: str) -> object: ...


@dataclass(frozen=True)
class _Placement:
    """Line maps from one commit to the head under review; ``left_known``: the left side
    could be mapped (the commit's merge base is known)."""

    maps: LineMaps
    moved: dict[tuple[str, ReviewSide], str]
    left_known: bool


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
        each placed by this head's paths, lines and hunks (renames followed, I2). Each comment
        is mapped from the commit it was written on, on its own side (N1). What changed since
        is outdated; what can't be mapped carries why (``unplaced``, N2): neither is ever
        placed by its old line.

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
        keys: dict[str, set[tuple[str, ReviewSide]]] = {}
        for comment in previous.comments:
            if comment.path is not None and comment.commit_sha is not None:
                keys.setdefault(comment.commit_sha, set()).add(
                    (comment.path, comment.side or "RIGHT")
                )
        finding_keys: set[tuple[str, ReviewSide]] = {
            (str(item["path"]), "RIGHT")
            for artifact in findings
            if isinstance(artifact, Mapping) and isinstance(artifact.get("findings"), list)
            for item in artifact["findings"]
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        }
        if followup.prev_head is not None and finding_keys:
            keys.setdefault(followup.prev_head, set()).update(finding_keys)
        causes: list[str] = []
        placements = {
            commit: await self._map_from(ref, commit, wanted, head_sha, merge_base, followup)
            for commit, wanted in keys.items()
            if commit != head_sha
        }
        comments = tuple(self._placed(c, head_sha, placements, causes) for c in previous.comments)
        found: tuple[PreviousFinding, ...] = ()
        if followup.prev_head is not None and followup.prev_head in placements:
            at = placements[followup.prev_head]
            if isinstance(at, str):
                causes.append(at)
                found = previous_findings(findings, {}, {}, unplaced=at)
            else:
                found = previous_findings(findings, at.maps, at.moved)
        steps = previous_steps(walkthrough, {})
        if walkthrough is not None:
            matches, why = await self._matches(ref, followup, head_sha, diff)
            if why is not None:
                causes.append(why)
            steps = previous_steps(walkthrough, matches, unplaced=why)
        error = "; ".join(dict.fromkeys(causes)) or None
        return replace(
            previous, comments=comments, steps=steps, findings=found, placement_error=error
        )

    async def _map_from(
        self,
        ref: PrRef,
        commit: str,
        keys: set[tuple[str, ReviewSide]],
        head_sha: str,
        merge_base: str,
        followup: FollowUp,
    ) -> _Placement | str:
        """Line maps from ``commit`` to ``head_sha`` for ``keys``: the right side between the
        two commits, the left side between their merge bases (``None`` when ``commit``'s isn't
        known). The cause, in words, when git can't read ``commit``."""
        try:
            await self._workspace.ensure_mirror(ref.repo)
            await self._fetch(ref.repo, [commit, head_sha])
            if commit == followup.prev_head:
                old_base = followup.prev_merge_base
            else:
                old_base = await self._workspace.merge_base(ref.repo, merge_base, commit)
            left = None if old_base is None else (old_base, merge_base)
            moved: dict[tuple[str, ReviewSide], str] = {}
            maps = await line_maps(
                self._workspace, ref.repo, {"RIGHT": (commit, head_sha), "LEFT": left}, keys, moved
            )
        except WorkspaceError as exc:  # secondary: unplaced, with the cause kept
            log.warning("review.rereview %s: nothing from %s placed: %s", ref, commit[:12], exc)
            return f"commit {commit[:12]} couldn't be read: {exc}"
        return _Placement(maps, moved, left is not None)

    @staticmethod
    def _placed(
        comment: PreviousComment,
        head_sha: str,
        placements: Mapping[str, _Placement | str],
        causes: list[str],
    ) -> PreviousComment:
        if comment.path is None:
            return comment
        if comment.commit_sha is None:
            why = "the commit it was written on isn't known"
        elif comment.commit_sha == head_sha:  # written on this very head: it is where it is
            return replace(
                comment,
                now_path=comment.path,
                now_line=comment.line,
                now_start_line=comment.start_line,
            )
        else:
            at = placements[comment.commit_sha]
            if isinstance(at, _Placement) and (comment.side != "LEFT" or at.left_known):
                return place(comment, at.maps, at.moved)
            why = at if isinstance(at, str) else "the base it was written against isn't known"
        causes.append(why)
        return replace(comment, now_path=comment.path, unplaced=why)

    async def _matches(
        self, ref: PrRef, followup: FollowUp, head_sha: str, diff: ParsedDiff
    ) -> tuple[dict[HunkRef, HunkRef], str | None]:
        """The previous review's hunks as this head's (previous to current), or why they
        can't be matched."""
        try:
            await self._workspace.ensure_mirror(ref.repo)
            old_diff = await self._previous_diff(ref.repo, followup)
            if old_diff is None or followup.prev_head is None:
                return {}, "the previous review's diff couldn't be rebuilt"
            renames = await self._workspace.renames(ref.repo, followup.prev_head, head_sha)
        except WorkspaceError as exc:  # secondary: unplaced, with the cause kept
            log.warning("review.rereview %s: previous hunks not matched: %s", ref, exc)
            return {}, f"the previous review's hunks couldn't be matched: {exc}"
        matched = match_hunks(old_diff, diff, renames)
        return {old: new for new, old in matched.items()}, None

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
                    # Posted on the round's head: its lines are that commit's.
                    written = tuple(replace(c, commit_sha=rnd.head_sha) for c in comments)
                    return PreviousReview("round", rnd.head_sha, written, rnd.id)
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
        comments = comments_from_threads(threads, viewer, body, review_commit=head or None)
        return PreviousReview("github", head, tuple(comments))

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
