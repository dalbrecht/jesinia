# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drafts and posting a review (spec §7): the only path from Jesinia to a GitHub write.

``preview`` builds exactly what would be posted for a round (the body and every inline
comment) with its warnings and a digest. ``post`` posts that, and only that: the caller
passes the digest of the preview the author confirmed, every warning must be confirmed, and
the review goes to GitHub as one atomic review against the round's head.

A post is recorded (``postings``) before GitHub is called. If the response is lost (a
timeout after GitHub took the review), a retry, with the same idempotency key or another,
first looks for the review on GitHub (by the viewer, on the round's commit, with the same
state and body, submitted since the attempt began) and adopts it instead of posting twice.

Bodies and tokens are never logged: only the round, the event and the review's URL.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Protocol

from jesinia.apps.review.domain.posting import (
    BODY_MAX,
    DRAFTS_MAX,
    EVENT_STATES,
    REVIEW_EVENTS,
    CommentableLines,
    CommentOnlyOnOwnPr,
    Draft,
    EmptyReview,
    InvalidDraftError,
    NeedsConfirmation,
    NoReviewRound,
    Posting,
    PostWarning,
    PreviewOutdated,
    ReviewedHeadGone,
    commentable_lines,
    compose_review,
    review_digest,
    review_warnings,
    same_body,
    seed_drafts,
)
from jesinia.apps.review.domain.queue import PrRef, QueueDomainError, RoundRecord
from jesinia.apps.review.domain.workspace import WorkspaceError
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
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
# GitHub's clock and ours may disagree: a review submitted this long before an attempt
# began still counts as that attempt's.
ADOPT_SKEW = timedelta(minutes=5)
_FILE_ERRORS = (GitHubError, GitHubPayloadError, PullRequestDomainError, QueueDomainError)


class PostingFailed(GitHubError):  # noqa: N818 - the error code clients see
    """GitHub didn't confirm the review. ``maybe_posted``: the request may have reached
    GitHub (its response was lost), so a retry looks for it before posting again; otherwise
    nothing was posted. The drafts are kept either way; ``__cause__`` is GitHub's error."""

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

    def viewer(self) -> str | None: ...

    def pr_files(self, ref: PrRef, head_sha: str) -> builtins.list[PrFile] | None: ...

    def drafts(self, round_id: int) -> builtins.list[Draft]: ...

    def review_summary(self, round_id: int) -> str: ...

    def seed_drafts(self, round_id: int, drafts: Sequence[Draft]) -> bool: ...

    def save_drafts(self, round_id: int, summary: str, drafts: Sequence[Draft]) -> None: ...

    def postings(self, round_id: int) -> builtins.list[Posting]: ...

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

    def fail_posting(self, posting_id: int, error: str) -> None: ...


@dataclass(frozen=True)
class DraftSet:
    """A round's review summary and drafts, as the author edits them."""

    round_id: int
    summary: str
    drafts: tuple[Draft, ...]


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

    # --- drafts ---------------------------------------------------------------------------

    async def drafts(self, round_id: int) -> DraftSet:
        """The round's summary and drafts; seeded from its findings the first time.

        Raises:
            NoReviewRound: no such round.
        """
        rnd = self._round(round_id)
        commentable, _ = await self._commentable(rnd)
        self._seed(rnd, commentable)
        return self._draft_set(round_id)

    async def save_drafts(self, round_id: int, summary: str, drafts: Sequence[Draft]) -> DraftSet:
        """Replace the round's summary and drafts (the author's edit, including deletions).

        Raises:
            NoReviewRound: no such round.
            InvalidDraftError: too many drafts, or a summary over the body limit.
        """
        self._round(round_id)
        if len(drafts) > DRAFTS_MAX:
            raise InvalidDraftError(f"a review takes at most {DRAFTS_MAX} drafts")
        if len(summary) > BODY_MAX:
            raise InvalidDraftError(f"the summary is at most {BODY_MAX} characters")
        self._store.save_drafts(round_id, summary, drafts)
        return self._draft_set(round_id)

    def _draft_set(self, round_id: int) -> DraftSet:
        return DraftSet(
            round_id, self._store.review_summary(round_id), tuple(self._store.drafts(round_id))
        )

    def _seed(self, rnd: RoundRecord, commentable: CommentableLines) -> None:
        """Seed the round's drafts from its findings, once."""
        found = self._store.artifact(rnd.id, "findings")
        correctness = self._store.artifact(rnd.id, "correctness")
        findings = [
            *_findings_of(correctness),
            *_findings_of(found),
        ]
        self._store.seed_drafts(rnd.id, seed_drafts(_unique(findings), commentable))

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
            ReviewedHeadGone: the reviewed commit is no longer on the PR.
            GitHubError: GitHub couldn't be read.
        """
        rnd = self._round(round_id)
        commentable, notes = await self._commentable(rnd)
        self._seed(rnd, commentable)
        live = await self._github.get(rnd.ref.repo, rnd.ref.number)
        newer_head = await self._newer_head(rnd, live)
        viewer = self._store.viewer() or await self._github.viewer()
        own_pr = live.author.lower() == viewer.lower()
        review = compose_review(
            self._store.review_summary(round_id), self._store.drafts(round_id), commentable
        )
        if event != "APPROVE" and not review.body:
            notes.append(f"GitHub needs a summary to post {event.lower().replace('_', ' ')}")
        return PostPreview(
            round_id=round_id,
            head_sha=rnd.head_sha,
            event=event,
            body=review.body,
            comments=review.comments,
            warnings=review_warnings(event, review, own_pr=own_pr, newer_head=newer_head),
            allowed_events=("COMMENT",) if own_pr else REVIEW_EVENTS,
            digest=review_digest(round_id, rnd.head_sha, event, review),
            notes=tuple(notes),
        )

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
            ReviewedHeadGone: the reviewed commit is no longer on the PR.
            PostingFailed: GitHub didn't confirm the review (with ``maybe_posted``).
            GitHubError: GitHub couldn't be read.
        """
        rnd = self._round(round_id)
        # One post per round at a time: two clicks with different keys can't both post.
        async with self._locks.setdefault(round_id, asyncio.Lock()):
            preview = await self.preview(round_id, event)
            if digest != preview.digest:
                raise PreviewOutdated("the review changed since its preview; preview it again")
            if event not in preview.allowed_events:
                raise CommentOnlyOnOwnPr("on your own PR, only a comment can be posted")
            if any(warning not in confirm for warning in preview.warnings):
                raise NeedsConfirmation(preview.warnings)
            if event != "APPROVE" and not preview.body:
                raise EmptyReview("write a summary: GitHub needs one to comment or request changes")
            done = await self._already_posted(rnd, preview)
            if done is not None:
                return done
            attempt = self._store.start_posting(
                round_id,
                event,
                key,
                preview.digest,
                preview.body,
                [asdict(comment) for comment in preview.comments],
                self._clock(),
            )
            return await self._send(rnd, attempt, preview)

    async def _send(self, rnd: RoundRecord, attempt: Posting, preview: PostPreview) -> str:
        try:
            url = await self._writer.create_review(
                rnd.ref.repo,
                rnd.ref.number,
                commit_sha=rnd.head_sha,
                event=preview.event,
                body=preview.body,
                comments=preview.comments,
            )
        except (GitHubRateLimitedError, GitHubRequestError) as exc:
            # No status: the request failed in transit and may have reached GitHub.
            unknown = exc.status is None
            if not unknown:
                self._store.fail_posting(attempt.id, str(exc))
            log.warning(
                "review.post round=%d event=%s failed (maybe posted: %s): %s",
                rnd.id,
                preview.event,
                unknown,
                exc.github_message if exc.status is not None else type(exc.__cause__).__name__,
            )
            raise PostingFailed(exc, maybe_posted=unknown) from exc
        except GitHubError as exc:  # no token: nothing left Jesinia
            self._store.fail_posting(attempt.id, str(exc))
            raise PostingFailed(exc, maybe_posted=False) from exc
        self._store.finish_posting(attempt.id, url)
        log.info("review.post round=%d event=%s posted %s", rnd.id, preview.event, url)
        return url

    async def _already_posted(self, rnd: RoundRecord, preview: PostPreview) -> str | None:
        """The URL of this very review if an earlier attempt posted it: one that was
        confirmed, or one whose response was lost but which GitHub holds (adopted now).
        Attempts that left nothing on GitHub are marked failed."""
        attempts = [p for p in self._store.postings(rnd.id) if p.digest == preview.digest]
        for attempt in attempts:
            if attempt.state == "posted" and attempt.review_url is not None:
                return attempt.review_url
        unsettled = [p for p in self._store.postings(rnd.id) if p.state == "started"]
        if not unsettled:
            return None
        viewer = (self._store.viewer() or await self._github.viewer()).lower()
        reviews = await self._github.reviews(rnd.ref.repo, rnd.ref.number)
        adopted = None
        for attempt in unsettled:
            found = next((r for r in reviews if _is_attempt(r, attempt, viewer, rnd)), None)
            if found is None or found.url is None:
                self._store.fail_posting(attempt.id, "no review was found on GitHub for it")
                continue
            self._store.finish_posting(attempt.id, found.url)
            log.info("review.post round=%d adopted %s after a lost response", rnd.id, found.url)
            if attempt.digest == preview.digest:
                adopted = found.url
        return adopted


def _is_attempt(review: ReviewSummary, attempt: Posting, viewer: str, rnd: RoundRecord) -> bool:
    return (
        review.author.lower() == viewer
        and review.commit_sha == rnd.head_sha
        and review.state == EVENT_STATES[attempt.event]
        and review.submitted_at is not None
        and review.submitted_at >= attempt.started_at - ADOPT_SKEW
        and same_body(review.body, attempt.body)
    )


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
