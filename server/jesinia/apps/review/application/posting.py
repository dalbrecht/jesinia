# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drafts and posting a review (spec §7): the only path from Jesinia to a GitHub write.

``preview`` builds exactly what would be posted for a round (the body and every inline
comment) with its warnings and a digest. ``post`` posts that, and only that: the caller
passes the digest of the preview the author confirmed, every warning must be confirmed, and
the review goes to GitHub as one atomic review against the round's head.

The digest is an integrity check ("what you saw is what posts"): a deterministic hash of
server state the client can read, not a nonce proving a preview was requested.

Exactly once (rulings R43, R44): a post is recorded (``postings``) before GitHub is called,
and the body it sends ends with the attempt's invisible marker. A lost response, a timeout
or any 5xx leaves the attempt ``uncertain``, and while a round has one nothing new is
posted: the next post, whatever its key or digest, first lists the PR's reviews. A review by
the viewer on the round's commit, submitted, whose body ends with that marker is adopted;
within ``POSTING_GRACE`` of the attempt a missing review means "retry later"
(``PostingStillUncertain``), after it the attempt is ``not_posted`` and the post goes on
(R48, R49). Drafts can't carry the marker's name at all, so no review can pass for one.

Drafts are read from the store only (carry f): where each one posts is worked out from the
cached file list on each read, and reported as pending when GitHub's list isn't cached.

Bodies and tokens are never logged: only the round, the event and the review's URL.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
import math
import sqlite3
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from jesinia.apps.review.domain.posting import (
    BODY_MAX,
    DRAFTS_MAX,
    MARKER_NOTE,
    REVIEW_EVENTS,
    SUBMITTED_STATES,
    UNSETTLED,
    CommentableLines,
    CommentOnlyOnOwnPr,
    Draft,
    EarlierReviewPosted,
    EmptyReview,
    InvalidDraftError,
    NeedsConfirmation,
    NoReviewRound,
    Posting,
    PostingInProgress,
    PostingState,
    PostingStillUncertain,
    PostWarning,
    PreviewOutdated,
    Review,
    ReviewedHeadGone,
    RoundFrozen,
    check_body_length,
    check_review_text,
    commentable_lines,
    compose_review,
    marked_body,
    needs_body,
    posting_marker,
    review_digest,
    review_warnings,
    seed_drafts,
)
from jesinia.apps.review.domain.queue import PrRef, QueueDomainError, RoundRecord
from jesinia.apps.review.domain.workspace import WorkspaceError
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRequestError,
)
from jesinia.services.github.domain.pr import (
    DraftComment,
    PrCommit,
    PrFile,
    PullRequest,
    PullRequestDomainError,
    ReviewEvent,
    ReviewSummary,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# What ``post_unless_posted`` found: posted by this attempt, or already posted another way.
PostedState = Literal["posted", "already_posted"]
# How long after an attempt began a missing review may still appear on GitHub (R49).
POSTING_GRACE = timedelta(minutes=3)
# The jobs whose findings become drafts.
_FINDING_JOBS: Final = ("correctness", "findings")
_FILE_ERRORS = (GitHubError, GitHubPayloadError, PullRequestDomainError, QueueDomainError)


class PostingFailed(GitHubError):  # noqa: N818 - the error code clients see
    """GitHub didn't confirm the review. ``maybe_posted``: the request may have reached
    GitHub (a lost response, a timeout, a 5xx), so the next post reconciles it before
    anything else is sent; otherwise GitHub refused it and nothing was posted. The drafts
    are kept either way; ``__cause__`` is GitHub's error."""

    def __init__(self, cause: Exception, *, maybe_posted: bool) -> None:
        outcome = (
            "GitHub's answer was lost, so the review may have been posted; retry to check"
            if maybe_posted
            else "nothing was posted"
        )
        super().__init__(f"posting the review failed ({outcome}): {cause}")
        self.maybe_posted = maybe_posted


class PostingGitHub(Protocol):
    """GitHub reads posting needs (``PrClient``); each may raise ``GitHubError``."""

    async def get(self, repo: str, number: int) -> PullRequest: ...

    async def commits_of(self, pr: PullRequest) -> tuple[PullRequest, list[PrCommit]]: ...

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]: ...

    async def viewer(self) -> str: ...


class ReviewWriter(Protocol):
    """GitHub's one write (``GitHubService.create_review``)."""

    async def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_sha: str,
        event: ReviewEvent,
        body: str,
        comments: Sequence[DraftComment],
    ) -> str: ...


class PostingFiles(Protocol):
    """GitHub's file list for a head, cached (``QueueService.files``); raises
    ``PrHeadMovedError`` when GitHub can no longer list that head."""

    async def files(self, repo: str, number: int, head_sha: str) -> builtins.list[PrFile]: ...


class PostingStore(Protocol):
    """Rounds, drafts and postings in ``review.sqlite``."""

    def round(self, round_id: int) -> RoundRecord | None: ...

    def latest_round(self, ref: PrRef) -> RoundRecord | None: ...

    def artifact(self, round_id: int, kind: str) -> object: ...

    def artifact_kinds(self, round_id: int) -> set[str]: ...

    def viewer(self) -> str | None: ...

    def pr_files(self, ref: PrRef, head_sha: str) -> builtins.list[PrFile] | None: ...

    def drafts(self, round_id: int) -> builtins.list[Draft]: ...

    def review_summary(self, round_id: int) -> str: ...

    def seed_drafts(self, round_id: int, drafts: Sequence[Draft]) -> bool: ...

    def save_drafts(self, round_id: int, summary: str, drafts: Sequence[Draft]) -> None: ...

    def postings(self, round_id: int) -> builtins.list[Posting]: ...

    def unsettled_posting_rounds(self) -> builtins.list[int]: ...

    def unsettled_rounds_of(self, ref: PrRef) -> builtins.list[int]: ...

    def start_posting(
        self,
        round_id: int,
        event: ReviewEvent,
        key: str,
        digest: str,
        body: str,
        comments: object,
        started_at: datetime,
    ) -> Posting: ...

    def finish_posting(self, posting_id: int, review_url: str) -> None: ...

    def settle_posting(self, posting_id: int, state: PostingState, error: str) -> None: ...


@dataclass(frozen=True)
class DraftSet:
    """A round's review summary and drafts, as the author edits them. ``in_body`` says, per
    draft, whether it posts in the review body (GitHub can't anchor it inline); it is
    ``None`` while GitHub's file list for the head isn't cached (``files_pending``).
    ``findings_pending``: the correctness pass or the findings job is still running, so
    their drafts aren't here yet; they are added when both have finished or failed."""

    round_id: int
    summary: str
    drafts: tuple[Draft, ...]
    in_body: tuple[bool, ...] | None = None
    findings_pending: bool = False
    # Finding drafts a save couldn't keep under DRAFTS_MAX (the author's come first).
    seeds_dropped: int = 0

    @property
    def files_pending(self) -> bool:
        return self.in_body is None


@dataclass(frozen=True)
class PostPreview:
    """Exactly what ``post`` would send for ``event``: the body and inline comments, the
    warnings to confirm, which events the PR allows, notes on anything degraded, and the
    digest ``post`` must be given back."""

    round_id: int
    head_sha: str
    event: ReviewEvent
    body: str
    comments: tuple[DraftComment, ...]
    warnings: tuple[PostWarning, ...]
    allowed_events: tuple[ReviewEvent, ...]
    digest: str
    notes: tuple[str, ...] = ()
    # The PR's head on GitHub when previewed (the newer-head warning names it, M3).
    pr_head_sha: str = ""


class PostingService:
    """The author's drafts and their one atomic review.

    Args:
        github: PR, commit, review and viewer reads (``PrClient``).
        writer: the review write (``GitHubService``).
        files: GitHub's file lists per head (``QueueService``), for where comments anchor.
        store: ``review.sqlite``.
        clock: the current UTC time.
    """

    def __init__(
        self,
        *,
        github: PostingGitHub,
        writer: ReviewWriter,
        files: PostingFiles,
        store: PostingStore,
        clock: Clock,
    ) -> None:
        self._github = github
        self._writer = writer
        self._files = files
        self._store = store
        self._clock = clock
        self._locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}

    async def round_for(self, ref: PrRef) -> int:
        """The PR's latest round, the one drafts and posts apply to.

        Raises:
            NoReviewRound: the PR has never had one.
        """
        found = self._store.latest_round(ref)
        if found is None:
            raise NoReviewRound(f"{ref} has no review round yet; open it first")
        return found.id

    def _round(self, round_id: int) -> RoundRecord:
        found = self._store.round(round_id)
        if found is None:
            raise NoReviewRound(f"no review round {round_id}")
        return found

    def _open_round(self, round_id: int) -> RoundRecord:
        """The round, if it can still be edited and posted.

        Raises:
            NoReviewRound: no such round.
            RoundFrozen: a round on the PR's new head took it over (spec §7).
        """
        found = self._round(round_id)
        if found.frozen_at is not None:
            raise RoundFrozen(round_id, found.successor_id)
        return found

    # --- drafts ---------------------------------------------------------------------------

    async def drafts(self, round_id: int) -> DraftSet:
        """The round's summary and drafts; seeded from its findings the first time.

        Raises:
            NoReviewRound: no such round.
        """
        rnd = self._round(round_id)
        return self._draft_set(rnd, pending=not self._seed(rnd))

    async def save_drafts(self, round_id: int, summary: str, drafts: Sequence[Draft]) -> DraftSet:
        """Replace the round's summary and drafts (the author's edit, including deletions).

        Raises:
            NoReviewRound: no such round.
            InvalidDraftError: too many drafts, or a summary over the body limit.
            RoundFrozen: a newer round took it over.
        """
        rnd = self._open_round(round_id)
        if len(drafts) > DRAFTS_MAX:
            raise InvalidDraftError(f"a review takes at most {DRAFTS_MAX} drafts")
        if len(summary) > BODY_MAX:
            raise InvalidDraftError(f"the summary is at most {BODY_MAX} characters")
        check_review_text(summary, drafts)
        # Findings already in are seeded first, so this edit (deletions included) replaces
        # them; findings still running are added next to it when they arrive.
        settled, seeded = self._seed_claim(rnd)
        # Drafts this call just seeded were never seen by the client (PR 3 final I2): the
        # author's list replaces what they had, and the new ones are kept after it, as many
        # as fit under DRAFTS_MAX; the rest are counted so the UI can say so.
        kept = seeded[: max(0, DRAFTS_MAX - len(drafts))]
        self._store.save_drafts(round_id, summary, [*drafts, *kept])
        return replace(
            self._draft_set(rnd, pending=not settled), seeds_dropped=len(seeded) - len(kept)
        )

    def _draft_set(self, rnd: RoundRecord, *, pending: bool) -> DraftSet:
        """The drafts from the store, each placed against the cached file list (never
        fetched here: a GET must not wait for GitHub)."""
        drafts = tuple(self._store.drafts(rnd.id))
        files = self._store.pr_files(rnd.ref, rnd.head_sha)
        placed = None
        if files is not None:
            commentable = commentable_lines(files)
            placed = tuple(not commentable.anchors(draft) for draft in drafts)
        return DraftSet(rnd.id, self._store.review_summary(rnd.id), drafts, placed, pending)

    def _findings_settled(self, rnd: RoundRecord) -> bool:
        """Whether the round's findings are all in: it finished cleanly, or the correctness
        pass and the findings job have each stored their output or failed."""
        if rnd.finished_at is not None and rnd.error is None:
            return True
        kinds = self._store.artifact_kinds(rnd.id)
        jobs = self._store.artifact(rnd.id, "jobs")
        failed = {
            kind
            for kind, entry in (jobs.items() if isinstance(jobs, Mapping) else ())
            if isinstance(entry, Mapping) and entry.get("state") == "failed"
        }
        return all(kind in kinds or kind in failed for kind in _FINDING_JOBS)

    def _seed(self, rnd: RoundRecord) -> bool:
        """Seed the round's drafts from its findings, once, as soon as they are all in
        (Codex round 2: seeding earlier would miss them for good). Returns whether they
        are in."""
        return self._seed_claim(rnd)[0]

    def _seed_claim(self, rnd: RoundRecord) -> tuple[bool, tuple[Draft, ...]]:
        """``_seed``, also returning the drafts this call seeded (empty unless it claimed
        the seeding)."""
        if not self._findings_settled(rnd):
            return False, ()
        found = self._store.artifact(rnd.id, "findings")
        correctness = self._store.artifact(rnd.id, "correctness")
        findings = [
            *_findings_of(correctness),
            *_findings_of(found),
        ]
        generated = tuple(seed_drafts(_unique(findings)))
        claimed = self._store.seed_drafts(rnd.id, generated)
        return True, generated if claimed else ()

    async def _commentable(self, rnd: RoundRecord) -> tuple[CommentableLines, list[str]]:
        """Where GitHub takes comments on the round's head: its cached file list, read once
        if missing. When GitHub can't list that head any more, nothing anchors inline (so
        every draft goes in the body) and a note says why."""
        files = self._store.pr_files(rnd.ref, rnd.head_sha)
        if files is None:
            try:
                files = await self._files.files(rnd.ref.repo, rnd.ref.number, rnd.head_sha)
            except (*_FILE_ERRORS, WorkspaceError) as exc:
                note = (
                    "GitHub's list of this head's files couldn't be read, so every draft is "
                    f"posted in the review body: {exc}"
                )
                return commentable_lines([]), [note]
        return commentable_lines(files), []

    # --- preview and post -----------------------------------------------------------------

    async def preview(self, round_id: int, event: ReviewEvent) -> PostPreview:
        """Exactly what posting ``event`` for the round would send, and its warnings.

        Raises:
            NoReviewRound: no such round.
            ReviewBodyTooLong: the composed body would be over GitHub's limit.
            ReviewedHeadGone: the reviewed commit is no longer on the PR.
            RoundFrozen: a newer round took it over: nothing can be posted from it.
            GitHubError: GitHub couldn't be read.
        """
        rnd = self._open_round(round_id)
        settled = self._seed(rnd)
        commentable, notes = await self._commentable(rnd)
        if not settled:
            notes.append(
                "the correctness pass or the findings job is still running; its drafts are "
                "added when it finishes"
            )
        live = await self._github.get(rnd.ref.repo, rnd.ref.number)
        newer_head = await self._newer_head(rnd, live)
        viewer = self._store.viewer() or await self._github.viewer()
        own_pr = live.author.lower() == viewer.lower()
        drafts = self._store.drafts(round_id)
        review = compose_review(self._store.review_summary(round_id), drafts, commentable)
        if outdated := sum(draft.outdated for draft in drafts):
            notes.append(
                f"{outdated} outdated draft{'s' if outdated != 1 else ''} (written on an earlier "
                "head, whose lines changed since) post in the review body, not inline: edit "
                "or delete them first if that isn't what you want"
            )
        check_body_length(review)
        if needs_body(event, review):
            what = event.lower().replace("_", " ")
            notes.append(f"GitHub needs a summary or an inline comment to post {what}")
        against = self._against_evidence(rnd)
        if event == "APPROVE" and against is not None:
            notes.append(against)
        for earlier in self._store.postings(round_id):
            if earlier.state == "posted":  # Post all, or another tab (final M2)
                notes.append(
                    f"This round's review was already posted ({earlier.event}, "
                    f"{earlier.review_url or _pr_url(rnd)}); posting again adds a second review"
                )
        notes.append(MARKER_NOTE)
        return PostPreview(
            round_id=round_id,
            head_sha=rnd.head_sha,
            event=event,
            body=review.body,
            comments=review.comments,
            warnings=review_warnings(
                event,
                review,
                own_pr=own_pr,
                newer_head=newer_head,
                against_evidence=against is not None,
            ),
            allowed_events=("COMMENT",) if own_pr else REVIEW_EVENTS,
            digest=review_digest(round_id, rnd.head_sha, event, review),
            notes=tuple(notes),
            pr_head_sha=live.head_sha,
        )

    def _against_evidence(self, rnd: RoundRecord) -> str | None:
        """Why approving goes against Jesinia's evidence (R54), or ``None`` when the round
        finished with an approve verdict: the verdict and its reasons (or that the review
        hasn't finished), then the CI, correctness and coverage evidence."""
        if rnd.finished_at is not None and rnd.error is None and rnd.verdict == "approve":
            return None
        if rnd.finished_at is None or rnd.error is not None or rnd.verdict is None:
            why = "the review hasn't finished, so there is no verdict to approve on"
        else:
            why = f"Jesinia's verdict is {rnd.verdict}"
        found = self._store.artifact(rnd.id, "verdict")
        reasons = found.get("reasons") if isinstance(found, Mapping) else None
        if isinstance(reasons, list) and reasons:
            why += " (" + "; ".join(map(str, reasons)) + ")"
        evidence = self._store.artifact(rnd.id, "evidence")
        if isinstance(evidence, Mapping):
            coverage = evidence.get("correctness_coverage")
            pct = coverage.get("pct") if isinstance(coverage, Mapping) else None
            read = "unknown" if pct is None else f"{pct}%"
            why += (
                f". Evidence: CI: {evidence.get('ci')}; correctness: "
                f"{evidence.get('correctness')}; coverage read: {read}"
            )
        return f"Approving goes against the evidence: {why}. Confirm to approve anyway."

    async def _newer_head(self, rnd: RoundRecord, live: PullRequest) -> bool:
        """Whether the PR has commits after the round's head.

        Raises:
            ReviewedHeadGone: the round's head isn't one of the PR's commits any more.
        """
        if live.head_sha == rnd.head_sha:
            return False
        listed, commits = await self._github.commits_of(live)
        if listed.commits_truncated or any(c.sha == rnd.head_sha for c in commits):
            return True  # GitHub may hold it past its list's cut: it decides on posting
        raise ReviewedHeadGone(
            f"{rnd.ref} no longer contains {rnd.head_sha[:12]}, the commit this round "
            f"reviewed (it is at {live.head_sha[:12]}); review the new head"
        )

    async def post(
        self,
        round_id: int,
        event: ReviewEvent,
        confirm: Collection[str],
        *,
        key: str,
        digest: str,
    ) -> str:
        """Post the previewed review for the round; return its URL.

        Raises:
            NoReviewRound: no such round.
            PreviewOutdated: ``digest`` isn't the current preview's (the drafts, the event
                or the round changed since).
            CommentOnlyOnOwnPr: approving or requesting changes on the author's own PR.
            NeedsConfirmation: a warning isn't in ``confirm``.
            EmptyReview: GitHub needs a body for this event.
            ReviewBodyTooLong: the composed body would be over GitHub's limit.
            EarlierReviewPosted: an earlier attempt turned out to be on GitHub and it isn't
                this digest and event, or it was an attempt of an earlier round of the PR
                (frozen by a head change); nothing was sent.
            ReviewedHeadGone: the reviewed commit is no longer on the PR.
            PostingFailed: GitHub didn't confirm the review (with ``maybe_posted``).
            RoundFrozen: a newer round took it over: nothing is posted from it.
            GitHubError: GitHub couldn't be read.
        """
        rnd = self._open_round(round_id)
        # One post per round at a time: two clicks with different keys can't both post.
        async with self._round_lock(round_id):
            return await self._post(rnd, event, confirm, key, digest)

    async def post_unless_posted(
        self,
        round_id: int,
        event: ReviewEvent,
        confirm: Collection[str],
        *,
        key: str,
        digest: str,
    ) -> tuple[str, PostedState]:
        """``post``, unless the round is posted already: checked under the round's posting
        lock, so a post that lands meanwhile (another key, another event) is never followed
        by a second review. Returns the URL and ``posted`` (now, or by this very attempt
        before: same key, digest and event) or ``already_posted`` (another way, or another
        version under this key: nothing was sent).

        Raises:
            As ``post``.
        """
        rnd = self._open_round(round_id)
        async with self._round_lock(round_id):
            posted = [p for p in self._store.postings(round_id) if p.state == "posted"]
            for earlier in posted:
                if earlier.key == key and earlier.digest == digest and earlier.event == event:
                    return earlier.review_url or _pr_url(rnd), "posted"
            if posted:
                return posted[-1].review_url or _pr_url(rnd), "already_posted"
            return await self._post(rnd, event, confirm, key, digest), "posted"

    async def reconcile_unsettled(self) -> int:
        """Settle every attempt left ``started`` (a crash) or ``uncertain`` (a lost
        response) once its grace period is over (M3), so a round shows as posted without
        the author posting again. Run by a background job, never by a read. A round whose
        attempt is still within its grace, or whose reviews GitHub can't list now, is left
        for the next run (logged, never raised). Returns how many rounds were settled."""
        settled = 0
        for round_id in self._store.unsettled_posting_rounds():
            rnd = self._round(round_id)
            try:
                async with self._round_lock(round_id):
                    await self._reconcile(rnd)
            except PostingStillUncertain:
                continue
            except (GitHubError, GitHubPayloadError) as exc:
                log.warning("review.reconcile round=%d left unsettled: %s", round_id, exc)
                continue
            except Exception:  # one round's surprise must not stop the others; cause logged
                log.exception("review.reconcile round=%d failed, left unsettled", round_id)
                continue
            settled += 1
        return settled

    @contextlib.asynccontextmanager
    async def _round_lock(self, round_id: int) -> AsyncIterator[None]:
        """The round's posting lock, dropped once nobody holds or waits for it."""
        self._lock_users[round_id] = self._lock_users.get(round_id, 0) + 1
        try:
            async with self._locks.setdefault(round_id, asyncio.Lock()):
                yield
        finally:
            self._lock_users[round_id] -= 1
            if not self._lock_users[round_id]:  # nobody holds or waits for it
                del self._lock_users[round_id], self._locks[round_id]

    async def _post(
        self, rnd: RoundRecord, event: ReviewEvent, confirm: Collection[str], key: str, digest: str
    ) -> str:
        await self._reconcile_earlier_rounds(rnd)
        adopted = await self._reconcile(rnd)
        if adopted is not None:
            # The earlier attempt is on GitHub: never a second review in this call, and
            # never reported as this one unless it is exactly this one (R47).
            url = adopted.review_url or _pr_url(rnd)
            if adopted.digest == digest and adopted.event == event:
                return url
            raise EarlierReviewPosted(url, adopted.event)
        preview = await self.preview(rnd.id, event)
        if digest != preview.digest:
            raise PreviewOutdated("the review changed since its preview; preview it again")
        if event not in preview.allowed_events:
            raise CommentOnlyOnOwnPr("on your own PR, only a comment can be posted")
        if any(warning not in confirm for warning in preview.warnings):
            raise NeedsConfirmation(preview.warnings)
        if needs_body(event, Review(preview.body, preview.comments)):
            raise EmptyReview(
                "write a summary or an inline comment: GitHub needs one to comment or "
                "request changes"
            )
        for earlier in self._store.postings(rnd.id):
            if earlier.state == "posted" and earlier.digest == preview.digest:
                return earlier.review_url or _pr_url(rnd)  # this very review, posted
        try:
            attempt = self._store.start_posting(
                rnd.id,
                event,
                key,
                preview.digest,
                preview.body,
                [asdict(comment) for comment in preview.comments],
                self._clock(),
            )
        except sqlite3.IntegrityError as exc:  # another process's attempt is in flight
            raise PostingInProgress(
                "another attempt to post this review is in progress; retry shortly"
            ) from exc
        if attempt.state == "posted":  # the key of a posted attempt: never sent again
            return attempt.review_url or _pr_url(rnd)
        return await self._send(rnd, attempt, preview)

    async def _reconcile_earlier_rounds(self, rnd: RoundRecord) -> None:
        """Settle the unsettled attempts of the PR's other rounds first (I2): a round frozen
        by a head change may hold a lost-response attempt that is on GitHub, and nothing is
        posted while it might be.

        Raises:
            EarlierReviewPosted: that attempt is on GitHub; nothing was sent.
            PostingStillUncertain: it may still appear (within its grace); retry later.
            GitHubError: the reviews couldn't be listed.
        """
        for other_id in self._store.unsettled_rounds_of(rnd.ref):
            if other_id == rnd.id:
                continue
            other = self._round(other_id)
            async with self._round_lock(other_id):
                adopted = await self._reconcile(other)
            if adopted is not None:
                raise EarlierReviewPosted(adopted.review_url or _pr_url(other), adopted.event)

    async def _send(self, rnd: RoundRecord, attempt: Posting, preview: PostPreview) -> str:
        try:
            url = await self._writer.create_review(
                rnd.ref.repo,
                rnd.ref.number,
                commit_sha=rnd.head_sha,
                event=preview.event,
                body=marked_body(preview.body, attempt.id),
                comments=preview.comments,
            )
        except GitHubRequestError as exc:
            # No status (in transit, a timeout) or a 5xx: GitHub may have stored it.
            uncertain = exc.status is None or exc.status >= 500
            state: PostingState = "uncertain" if uncertain else "failed"
            self._store.settle_posting(attempt.id, state, str(exc))
            log.warning(
                "review.post round=%d posting=%d event=%s %s: %s",
                rnd.id,
                attempt.id,
                preview.event,
                state,
                exc.github_message if exc.status is not None else type(exc.__cause__).__name__,
            )
            raise PostingFailed(exc, maybe_posted=uncertain) from exc
        except GitHubError as exc:  # no token: nothing left Jesinia
            self._store.settle_posting(attempt.id, "failed", str(exc))
            raise PostingFailed(exc, maybe_posted=False) from exc
        self._store.finish_posting(attempt.id, url)
        log.info("review.post round=%d posting=%d posted %s", rnd.id, attempt.id, url)
        return url

    async def _reconcile(self, rnd: RoundRecord) -> Posting | None:
        """Settle the round's attempts whose outcome isn't known (a crash leaves
        ``started``, a lost response ``uncertain``): adopt the review carrying an attempt's
        marker, by the viewer on the round's commit, or mark the attempt ``not_posted``.
        Returns the adopted attempt (with its URL), if any.

        Raises:
            GitHubError: the reviews couldn't be listed (the attempt stays unsettled, so
                nothing is posted until they can be).
        """
        unsettled = [p for p in self._store.postings(rnd.id) if p.state in UNSETTLED]
        if not unsettled:
            return None
        viewer = (self._store.viewer() or await self._github.viewer()).lower()
        reviews = await self._github.reviews(rnd.ref.repo, rnd.ref.number)
        adopted = None
        now = self._clock()
        for attempt in unsettled:
            marker = posting_marker(attempt.id)
            found = next(
                (
                    r
                    for r in reviews
                    if r.author.lower() == viewer
                    and r.commit_sha == rnd.head_sha
                    and r.state in SUBMITTED_STATES
                    and r.body.rstrip().endswith(marker)
                ),
                None,
            )
            if found is None:
                waited = now - attempt.started_at
                if waited < POSTING_GRACE:
                    # GitHub may still be storing it, or its list may lag (R49).
                    left = POSTING_GRACE - waited
                    raise PostingStillUncertain(max(1, math.ceil(left.total_seconds())))
                self._store.settle_posting(attempt.id, "not_posted", "not on GitHub")
                continue
            # GitHub's list may lack a review's page; the PR's page then stands in for it.
            url = found.url or _pr_url(rnd)
            self._store.finish_posting(attempt.id, url)
            adopted = replace(attempt, state="posted", review_url=url)
            log.info("review.post round=%d posting=%d adopted %s", rnd.id, attempt.id, url)
        return adopted


def _pr_url(rnd: RoundRecord) -> str:
    """The PR's page on GitHub."""
    return f"https://github.com/{rnd.ref.repo}/pull/{rnd.ref.number}"


def _findings_of(artifact: object) -> list[object]:
    items = artifact.get("findings") if isinstance(artifact, dict) else None
    return list(items) if isinstance(items, list) else []


def _unique(findings: Sequence[object]) -> list[object]:
    """The correctness pass and the findings job often report one issue twice."""
    seen: list[object] = []
    for item in findings:
        if item not in seen:
            seen.append(item)
    return seen
