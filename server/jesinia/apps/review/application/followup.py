# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebases and head changes (spec §4.1, §7): what a PR's new head needs after the author's
previous review, and the drafts a new round carries over from the head before.

``classify`` finds the author's previous review (their latest review on GitHub, recognised
as Jesinia's own round by its posting marker), rebuilds both diffs in the local mirror and
compares them per file with ``git patch-id --verbatim`` (whitespace counts). What is stored
is the comparison by content; the CI rule is applied each time a follow-up is read.
``refresh`` does that once per new head, in the background after each sync; a head that
can't be checked (GitHub or git failed) is recorded as unchecked, with the cause, and is
checked again on the next run: never classified from a stale or partial view.

``reanchor`` finishes a head change: the drafts a new round took over (outdated until then)
move to the lines their text is on at the new head, or stay outdated.

Secondary work: every failure here is logged with its cause and isolated to its PR; the
queue, the pipeline and posting go on without it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Final, Protocol

from jesinia.apps.review.domain.followup import (
    FollowUp,
    LineMaps,
    PrevReview,
    compare,
    effective_followup,
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

    async def renames(self, repo: str, old_sha: str, new_sha: str) -> dict[str, str]: ...


class FollowUpStore(Protocol):
    """``review.sqlite``."""

    def viewer(self) -> str | None: ...

    def pr(self, ref: PrRef) -> StoredPr | None: ...

    def prs(self, include_dismissed: bool = False) -> list[StoredPr]: ...

    def repo_configs(self) -> dict[str, RepoConfig]: ...

    def round(self, round_id: int) -> RoundRecord | None: ...

    def posted_rounds(self, ref: PrRef) -> list[tuple[RoundRecord, Posting]]: ...

    def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None: ...

    def put_followup(self, ref: PrRef, head_sha: str, followup: FollowUp) -> None: ...

    def carried_drafts(self, round_id: int) -> list[tuple[Draft, str, str]]: ...

    def reanchor_drafts(self, round_id: int, drafts: list[Draft]) -> int: ...


class LineMapWorkspace(Protocol):
    """What mapping lines between two commits reads (``GitWorkspace``)."""

    async def show_file(self, repo: str, sha: str, path: str, *, limit: int) -> bytes | None: ...

    async def renames(self, repo: str, old_sha: str, new_sha: str) -> dict[str, str]: ...


async def line_maps(
    workspace: LineMapWorkspace,
    repo: str,
    commits: Mapping[ReviewSide, tuple[str, str] | None],
    keys: Collection[tuple[str, ReviewSide]],
    moved: dict[tuple[str, ReviewSide], str],
) -> LineMaps:
    """Old line to new line for each ``(path, side)`` in ``keys``, between the side's two
    commits (``commits``: the heads for the right side, the merge bases for the left),
    following renames git's ``-M`` finds between them (Task 11 M3); ``moved`` gets the new
    path of each renamed one. A side without commits, or a file that isn't at both or is too
    large, maps to ``None`` (whatever sits on it is outdated, never guessed).

    Raises:
        WorkspaceError: git couldn't list renames or read a file.
    """
    renamed: dict[ReviewSide, dict[str, str]] = {}
    maps: dict[tuple[str, ReviewSide], Mapping[int, int] | None] = {}
    for path, side in sorted(keys):
        pair = commits.get(side)
        if pair is None:
            maps[(path, side)] = None
            continue
        old, new = pair
        if side not in renamed:
            renamed[side] = await workspace.renames(repo, old, new)
        new_path = renamed[side].get(path, path)
        if new_path != path:
            moved[(path, side)] = new_path
        maps[(path, side)] = await _line_map(workspace, repo, old, new, path, new_path)
    return maps


async def _line_map(
    workspace: LineMapWorkspace, repo: str, old: str, new: str, path: str, new_path: str
) -> dict[int, int] | None:
    try:
        before = await workspace.show_file(repo, old, path, limit=REANCHOR_FILE_LIMIT)
        after = await workspace.show_file(repo, new, new_path, limit=REANCHOR_FILE_LIMIT)
    except MirrorFileTooLargeError:
        return None
    if before is None or after is None:
        return None
    old_text, new_text = before.decode(errors="replace"), after.decode(errors="replace")
    return await asyncio.to_thread(line_map, old_text, new_text)


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
        """What ``head_sha`` needs, once checked, under the PR's CI as cached now (C2)."""
        found = self._store.followup(ref, head_sha)
        stored = self._store.pr(ref)
        if found is None or stored is None or stored.pr.head_sha != head_sha:
            return found
        return effective_followup(found, stored.pr.ci_state)

    # --- the previous review --------------------------------------------------------------

    async def previous_review(self, stored: StoredPr) -> PrevReview | None:
        """The author's latest review of the PR (spec §4.1): their newest submitted review on
        GitHub, read as a stored round when it carries that round's posting marker (a review
        GitHub has since dismissed counts as the verdict Jesinia posted).

        There is no fallback when GitHub can't be read (I1): a round Jesinia posted may not be
        the author's latest verdict (they may have requested changes on GitHub since).

        Raises:
            GitHubError: GitHub's reviews couldn't be read.
        """
        ref, pr = stored.pr.ref, stored.pr
        posted = self._store.posted_rounds(ref)
        viewer = (self._store.viewer() or await self._github.viewer()).lower()
        reviews = await self._github.reviews(ref.repo, ref.number)
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
        """Compare the PR's current head with the author's previous review, store that and
        return it under the head's CI now.

        Raises:
            GitHubError: GitHub's reviews couldn't be read.
            WorkspaceError: the current head's merge base couldn't be computed (it must be
                in the mirror: a failure here is transient, so nothing is stored).
        """
        pr = stored.pr
        prev = await self.previous_review(stored)
        old_ids = new_ids = None
        if prev is not None and prev.head_sha != pr.head_sha:
            await self._workspace.ensure_mirror(pr.ref.repo)
            await self._fetch(pr.ref.repo, [pr.base_sha, pr.head_sha])
            base = await self._workspace.merge_base(pr.ref.repo, pr.base_sha, pr.head_sha)
            new_ids = await self._patch_ids(pr.ref.repo, base, pr.head_sha)
            if prev.merge_base is not None:
                old_ids = await self._patch_ids(pr.ref.repo, prev.merge_base, prev.head_sha)
        followup = compare(prev, pr, old_ids, new_ids)
        self._store.put_followup(pr.ref, pr.head_sha, followup)
        log.info("review.followup %s@%s: %s", pr.ref, pr.head_sha[:12], followup.kind)
        return effective_followup(followup, pr.ci_state)

    async def _patch_ids(self, repo: str, base: str, head: str) -> dict[str, str] | None:
        try:
            return await self._workspace.patch_ids(repo, base, head)
        except WorkspaceError as exc:
            log.warning("review.followup %s: no patch ids for %s: %s", repo, head[:12], exc)
            return None

    async def refresh(self) -> int:
        """Check every queued PR head not checked yet (or that couldn't be checked last
        time); return how many were classified. A PR in a suggested repo the author didn't
        load is skipped (it is never mirrored, R29a). One whose check fails is recorded as
        unchecked with its cause (shown on its row) and tried again next time."""
        configs = self._store.repo_configs()
        done = 0
        for stored in self._store.prs():
            done += await self._check(stored, configs.get(stored.pr.ref.repo))
        return done

    async def check(self, ref: PrRef) -> None:
        """Check ``ref``'s current head now, as ``refresh`` would (the author fetched it
        again to review it: its follow-up must be known before it opens, Task 11 M7). A
        failure is recorded as unchecked, with its cause, never raised."""
        stored = self._store.pr(ref)
        if stored is not None:
            await self._check(stored, self._store.repo_configs().get(ref.repo))

    async def _check(self, stored: StoredPr, config: RepoConfig | None) -> int:
        """Classify ``stored``'s head unless it is known, unreadable or not consented to
        (R29a); 1 when it was classified."""
        ref, head = stored.pr.ref, stored.pr.head_sha
        known = self._store.followup(ref, head)
        if stored.fetch_error is not None or (known is not None and known.error is None):
            return 0
        if config is not None and config.suggested and not stored.loaded:
            return 0
        try:
            await self.classify(stored)
        except Exception as exc:  # one PR's failure must not stop the others; cause kept
            log.warning("review.followup %s left unchecked: %s", ref, exc, exc_info=exc)
            cause = str(exc) or type(exc).__name__
            self._store.put_followup(ref, head, FollowUp("new", error=cause))
            return 0
        return 1

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
            WorkspaceError: the mirror itself couldn't be set up; the drafts stay outdated and
                pending, so the next preparation of the round tries again. An earlier head
                that can't be read only settles its own drafts as outdated.
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
            moved: dict[tuple[str, ReviewSide], str] = {}
            try:
                maps = await self._maps(
                    repo, old_head, old_base, head_sha, merge_base, drafts, moved
                )
            except WorkspaceError as exc:
                # The earlier head can't be read (force-pushed away, say): its drafts stay
                # outdated for good rather than failing every preparation (M2).
                log.warning("review.followup %s: drafts from %s stay outdated: %s",
                            repo, old_head[:12], exc)  # fmt: skip
                maps = {}
            settled.extend(reanchor(draft, maps, moved) for draft in drafts)
        return self._store.reanchor_drafts(round_id, settled)

    async def _maps(
        self,
        repo: str,
        old_head: str,
        old_base: str,
        head_sha: str,
        merge_base: str,
        drafts: list[Draft],
        moved: dict[tuple[str, ReviewSide], str],
    ) -> LineMaps:
        """Each draft file's line map per side, following renames between the two commits
        (git's ``-M``, Task 11 M3); ``moved`` gets the new path of each renamed one."""
        old_merge_base = await self._workspace.merge_base(repo, old_base, old_head)
        commits: dict[ReviewSide, tuple[str, str] | None] = {
            "RIGHT": (old_head, head_sha),
            "LEFT": (old_merge_base, merge_base),
        }
        keys = {(d.path, d.side or "RIGHT") for d in drafts}
        return await line_maps(self._workspace, repo, commits, keys, moved)
