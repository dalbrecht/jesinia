# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebases and head changes (spec §4.1, §7): what a PR's new head needs after the author's
previous review, and the drafts a new round carries over from the head before.

``classify`` finds the author's previous review (their latest review on GitHub, recognised
as Jesinia's own by its posting marker, or a stored posted round when GitHub can't be read),
rebuilds both diffs in the local mirror and compares them per file with
``git patch-id --stable``. ``refresh`` does that once per new head, in the background after
each sync; a PR whose classification fails is left for the next run, never guessed.

``reanchor`` finishes a head change: the drafts a new round took over (outdated until then)
move to the lines their text is on at the new head, or stay outdated.

Secondary work: every failure here is logged with its cause and isolated to its PR; the
queue, the pipeline and posting go on without it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from jesinia.apps.review.domain.followup import (
    FollowUp,
    LineMaps,
    PrevReview,
    classify,
    line_map,
    reanchor,
    verdict_of_event,
    verdict_of_review_state,
)
from jesinia.apps.review.domain.posting import Draft, Posting, posting_marker
from jesinia.apps.review.domain.queue import PrRef, RepoConfig, RoundRecord, StoredPr
from jesinia.apps.review.domain.workspace import MirrorFileTooLargeError, WorkspaceError
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import GitHubError
from jesinia.services.github.domain.pr import ReviewSide, ReviewSummary

log = logging.getLogger(__name__)

# The largest file re-anchoring reads at either head; a larger one leaves its drafts outdated.
REANCHOR_FILE_LIMIT: Final = 2 * 1024 * 1024
_GITHUB_ERRORS = (GitHubError, GitHubPayloadError)

After = Callable[[], Awaitable[None]]


class FollowUpGitHub(Protocol):
    """GitHub reads (``PrClient``); each may raise ``GitHubError``."""

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]: ...

    async def viewer(self) -> str: ...


class FollowUpWorkspace(Protocol):
    """The local mirror (``GitWorkspace``); each may raise ``WorkspaceError``."""

    async def ensure_mirror(self, repo: str) -> None: ...

    async def fetch(self, repo: str, refs: list[str]) -> None: ...

    async def merge_base(self, repo: str, a: str, b: str) -> str: ...

    async def patch_ids(self, repo: str, base_sha: str, head_sha: str) -> dict[str, str]: ...

    async def show_file(self, repo: str, sha: str, path: str, *, limit: int) -> bytes | None: ...


class FollowUpStore(Protocol):
    """``review.sqlite``."""

    def viewer(self) -> str | None: ...

    def prs(self, include_dismissed: bool = False) -> list[StoredPr]: ...

    def repo_configs(self) -> dict[str, RepoConfig]: ...

    def round(self, round_id: int) -> RoundRecord | None: ...

    def posted_rounds(self, ref: PrRef) -> list[tuple[RoundRecord, Posting]]: ...

    def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None: ...

    def put_followup(self, ref: PrRef, head_sha: str, followup: FollowUp) -> None: ...

    def carried_drafts(self, round_id: int) -> list[tuple[Draft, str, str]]: ...

    def reanchor_drafts(self, round_id: int, drafts: list[Draft]) -> int: ...


class FollowUps:
    """Follow-ups on new heads.

    Args:
        github: reviews and the viewer (``PrClient``).
        workspace: the local mirror (``GitWorkspace``).
        store: ``review.sqlite``.
        after: awaited after a background refresh classified anything (the scheduler's
            tick, so a still-approved head isn't pre-reviewed).
    """

    def __init__(
        self,
        *,
        github: FollowUpGitHub,
        workspace: FollowUpWorkspace,
        store: FollowUpStore,
        after: After | None = None,
    ) -> None:
        self._github = github
        self._workspace = workspace
        self._store = store
        self._after = after
        self._task: asyncio.Task[None] | None = None

    def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None:
        """What ``head_sha`` needs, once classified."""
        return self._store.followup(ref, head_sha)

    # --- the previous review --------------------------------------------------------------

    async def previous_review(self, stored: StoredPr) -> PrevReview | None:
        """The author's latest review of the PR (spec §4.1): their newest submitted review on
        GitHub, a stored round when it carries that round's posting marker (a review GitHub
        has since dismissed counts as the verdict Jesinia posted), or, when GitHub's reviews
        can't be read, the latest round Jesinia posted.

        Raises:
            GitHubError: GitHub couldn't be read and no round of the PR was posted.
        """
        ref, pr = stored.pr.ref, stored.pr
        posted = self._store.posted_rounds(ref)
        try:
            viewer = (self._store.viewer() or await self._github.viewer()).lower()
            reviews = await self._github.reviews(ref.repo, ref.number)
        except _GITHUB_ERRORS as exc:
            if not posted:
                raise
            log.warning("review.followup %s: GitHub's reviews unread, using rounds: %s", ref, exc)
            rnd, posting = posted[-1]
            return await self._from_round(rnd, posting)
        marked = {posting_marker(posting.id): (rnd, posting) for rnd, posting in posted}
        for review in reversed(reviews):
            if review.author.lower() != viewer or review.commit_sha is None:
                continue
            body = review.body.rstrip()
            ours = next((found for mark, found in marked.items() if body.endswith(mark)), None)
            if ours is not None:
                return await self._from_round(*ours)
            verdict = verdict_of_review_state(review.state)
            if verdict is not None:
                base = await self._merge_base(ref.repo, pr.base_sha, review.commit_sha)
                return PrevReview(review.commit_sha, base, verdict, "github")
        return None

    async def _from_round(self, rnd: RoundRecord, posting: Posting) -> PrevReview:
        base = await self._merge_base(rnd.ref.repo, rnd.base_sha, rnd.head_sha)
        return PrevReview(rnd.head_sha, base, verdict_of_event(posting.event), "round", rnd.id)

    async def _merge_base(self, repo: str, base_sha: str, head_sha: str) -> str | None:
        """The reviewed head's merge base, or ``None`` when that commit can't be read (it was
        force-pushed away and is gone from GitHub and the mirror)."""
        try:
            await self._workspace.ensure_mirror(repo)
            await self._fetch(repo, [base_sha, head_sha])
            return await self._workspace.merge_base(repo, base_sha, head_sha)
        except WorkspaceError as exc:
            log.warning("review.followup %s: no merge base for %s: %s", repo, head_sha[:12], exc)
            return None

    async def _fetch(self, repo: str, shas: list[str]) -> None:
        """Fetch ``shas`` that may already be in the mirror; a failure is left to the reads
        that need them, which say what is missing."""
        try:
            await self._workspace.fetch(repo, sorted(set(shas)))
        except WorkspaceError as exc:
            log.info("review.followup %s: fetch of %d commits failed: %s", repo, len(shas), exc)

    # --- classifying a head ---------------------------------------------------------------

    async def classify(self, stored: StoredPr) -> FollowUp:
        """Classify the PR's current head, store it and return it.

        Raises:
            GitHubError: GitHub couldn't be read and no round of the PR was posted.
        """
        pr = stored.pr
        prev = await self.previous_review(stored)
        old_ids = new_ids = None
        if prev is not None and prev.head_sha != pr.head_sha:
            base = await self._merge_base(pr.ref.repo, pr.base_sha, pr.head_sha)
            if base is not None:
                new_ids = await self._patch_ids(pr.ref.repo, base, pr.head_sha)
            if prev.merge_base is not None:
                old_ids = await self._patch_ids(pr.ref.repo, prev.merge_base, prev.head_sha)
        followup = classify(prev, pr, old_ids, new_ids, pr.ci_state)
        self._store.put_followup(pr.ref, pr.head_sha, followup)
        log.info("review.followup %s@%s: %s", pr.ref, pr.head_sha[:12], followup.kind)
        return followup

    async def _patch_ids(self, repo: str, base: str, head: str) -> dict[str, str] | None:
        try:
            return await self._workspace.patch_ids(repo, base, head)
        except WorkspaceError as exc:
            log.warning("review.followup %s: no patch ids for %s: %s", repo, head[:12], exc)
            return None

    async def refresh(self) -> int:
        """Classify every queued PR head not classified yet; return how many were. A PR in a
        suggested repo the author didn't load is skipped (it is never mirrored, R29a); one
        whose classification fails is logged and tried again next time."""
        configs = self._store.repo_configs()
        done = 0
        for stored in self._store.prs():
            ref, head = stored.pr.ref, stored.pr.head_sha
            config = configs.get(ref.repo)
            if stored.fetch_error is not None or self._store.followup(ref, head) is not None:
                continue
            if config is not None and config.suggested and not stored.loaded:
                continue
            try:
                await self.classify(stored)
            except Exception as exc:  # one PR's failure must not stop the others; cause logged
                log.warning("review.followup %s left unclassified: %s", ref, exc, exc_info=exc)
                continue
            done += 1
        return done

    def start_refresh(self) -> None:
        """Run ``refresh`` in the background (one at a time), then ``after`` if it classified
        anything."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._refresh_then_tick(), name="review-followups")

    async def _refresh_then_tick(self) -> None:
        if await self.refresh() and self._after is not None:
            await self._after()

    async def drain(self) -> None:
        """Wait for a running background refresh to end."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def stop(self) -> None:
        """Cancel a running background refresh."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # --- carried drafts -------------------------------------------------------------------

    async def reanchor(self, round_id: int, head_sha: str, merge_base: str) -> int:
        """Re-anchor the drafts round ``round_id`` carried over from an earlier head onto
        ``head_sha`` (whose merge base is ``merge_base``): right-side lines by the two heads'
        files, left-side lines by the two merge bases' files. Returns how many drafts were
        settled (moved, or confirmed outdated).

        Raises:
            WorkspaceError: git failed; the drafts stay outdated and pending, so the next
                preparation of the round tries again.
        """
        pending = self._store.carried_drafts(round_id)
        rnd = self._store.round(round_id)
        if not pending or rnd is None:
            return 0
        repo = rnd.ref.repo
        await self._workspace.ensure_mirror(repo)
        groups: dict[tuple[str, str], list[Draft]] = defaultdict(list)
        for draft, old_head, old_base in pending:
            groups[(old_head, old_base)].append(draft)
        settled: list[Draft] = []
        for (old_head, old_base), drafts in groups.items():
            await self._fetch(repo, [old_head, old_base, head_sha, merge_base])
            old_merge_base = await self._workspace.merge_base(repo, old_base, old_head)
            commits: dict[ReviewSide, tuple[str, str]] = {
                "RIGHT": (old_head, head_sha),
                "LEFT": (old_merge_base, merge_base),
            }
            maps: LineMaps = {
                (path, side): await self._line_map(repo, *commits[side], path)
                for path, side in {(d.path, d.side or "RIGHT") for d in drafts}
            }
            settled.extend(reanchor(draft, maps) for draft in drafts)
        return self._store.reanchor_drafts(round_id, settled)

    async def _line_map(self, repo: str, old: str, new: str, path: str) -> dict[int, int] | None:
        """Old line to new line for ``path`` between two commits; ``None`` when the file isn't
        at both, or is too large to compare."""
        try:
            before = await self._workspace.show_file(repo, old, path, limit=REANCHOR_FILE_LIMIT)
            after = await self._workspace.show_file(repo, new, path, limit=REANCHOR_FILE_LIMIT)
        except MirrorFileTooLargeError:
            return None
        if before is None or after is None:
            return None
        old_text, new_text = before.decode(errors="replace"), after.decode(errors="replace")
        return await asyncio.to_thread(line_map, old_text, new_text)
