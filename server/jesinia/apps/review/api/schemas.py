# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response bodies of ``/api/review`` (the generated web client's types)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from pydantic import BaseModel, Field, JsonValue

from jesinia.apps.review.application.pipeline import OpenResult
from jesinia.apps.review.application.posting import DraftSet, PostPreview
from jesinia.apps.review.application.reviews import PrView, ReviewSettings
from jesinia.apps.review.domain.gates import CiState, Depth
from jesinia.apps.review.domain.posting import (
    BODY_MAX,
    DRAFTS_MAX,
    PATH_MAX,
    Draft,
    PostWarning,
)
from jesinia.apps.review.domain.queue import (
    AuthorKind,
    AutoReviewState,
    EvidenceSummary,
    PreReviewMode,
    PreReviewState,
    PrKind,
    QueueRow,
    QueueView,
    RepoConfig,
    RoundRecord,
    SyncStatus,
)
from jesinia.services.github.domain.pr import DraftComment, ReviewEvent, ReviewSide

REF_MAX = 500
REPO_MAX = 140
RUNNER_MAX = 64
REVIEWER_MAX = 100


class EvidenceOut(BaseModel):
    ci: str
    tests: str | None
    findings: dict[str, int]

    @classmethod
    def of(cls, evidence: EvidenceSummary) -> EvidenceOut:
        return cls(ci=evidence.ci, tests=evidence.tests, findings=dict(evidence.findings))


class QueueRowOut(BaseModel):
    """A queue row (spec §2 "Each row shows"); ``title`` is the PR author's text."""

    repo: str
    display_repo: str
    number: int
    title: str
    url: str
    author: str
    author_kind: AuthorKind
    created_at: datetime
    size_lines: int
    files: int
    kind: PrKind
    depth: Depth
    ci_state: CiState
    auto_review_state: AutoReviewState
    review_comment_count: int
    bot_review_count: int
    human_review_count: int
    review_requested: bool
    prereview_state: PreReviewState
    prereview_reason: str
    recommendation: str | None
    recommendation_stale: bool
    evidence: EvidenceOut | None
    coverage_pct: float | None
    nothing_to_walk: bool = Field(
        description="The latest review's diff changes no line: show that, not 100%"
    )
    loaded: bool
    dismissed: bool
    head_sha: str
    fetch_error: str | None
    fetch_error_at: datetime | None
    suggested: bool

    @classmethod
    def of(cls, row: QueueRow) -> QueueRowOut:
        return cls(
            repo=row.repo,
            display_repo=row.display_repo,
            number=row.number,
            title=row.title,
            url=row.url,
            author=row.author,
            author_kind=row.author_kind,
            created_at=row.created_at,
            size_lines=row.size_lines,
            files=row.files,
            kind=row.kind,
            depth=row.depth,
            ci_state=row.ci_state,
            auto_review_state=row.auto_review_state,
            review_comment_count=row.review_comment_count,
            bot_review_count=row.bot_review_count,
            human_review_count=row.human_review_count,
            review_requested=row.review_requested,
            prereview_state=row.prereview_state,
            prereview_reason=row.prereview_reason,
            recommendation=row.recommendation,
            recommendation_stale=row.recommendation_stale,
            evidence=None if row.evidence is None else EvidenceOut.of(row.evidence),
            coverage_pct=row.coverage_pct,
            nothing_to_walk=row.nothing_to_walk,
            loaded=row.loaded,
            dismissed=row.dismissed,
            head_sha=row.head_sha,
            fetch_error=row.fetch_error,
            fetch_error_at=row.fetch_error_at,
            suggested=row.suggested,
        )


class SyncOut(BaseModel):
    synced_at: datetime | None
    syncing: bool
    sync_error: str | None

    @classmethod
    def of(cls, status: SyncStatus) -> SyncOut:
        return cls(synced_at=status.synced_at, syncing=status.syncing, sync_error=status.sync_error)


class QueueOut(BaseModel):
    """The cached queue; a running or failed sync is reported here, never by waiting."""

    rows: list[QueueRowOut]
    synced_at: datetime | None
    syncing: bool
    sync_error: str | None
    suggested_repos: list[str]

    @classmethod
    def of(cls, view: QueueView) -> QueueOut:
        return cls(
            rows=[QueueRowOut.of(row) for row in view.rows],
            synced_at=view.synced_at,
            syncing=view.syncing,
            sync_error=view.sync_error,
            suggested_repos=list(view.suggested_repos),
        )


class LoadIn(BaseModel):
    ref: str = Field(
        min_length=1,
        max_length=REF_MAX,
        description="A PR URL, owner/repo#n, or a /review/<owner>/<repo>/<n> link",
    )


class RoundOut(BaseModel):
    id: int
    head_sha: str
    base_sha: str
    depth: Depth
    started_at: datetime
    finished_at: datetime | None
    verdict: str | None
    error: str | None
    posted_review_url: str | None
    done: bool

    @classmethod
    def of(cls, rnd: RoundRecord) -> RoundOut:
        return cls(
            id=rnd.id,
            head_sha=rnd.head_sha,
            base_sha=rnd.base_sha,
            depth=rnd.depth,
            started_at=rnd.started_at,
            finished_at=rnd.finished_at,
            verdict=rnd.verdict,
            error=rnd.error,
            posted_review_url=rnd.posted_review_url,
            done=rnd.done,
        )


class PrOut(BaseModel):
    """An opened PR from the cache: its row, its latest round and every artifact of it."""

    pr: QueueRowOut
    round: RoundOut | None
    artifacts: dict[str, JsonValue] = Field(
        description="The round's artifacts by kind (agent output is the PR's to influence)"
    )
    stale: bool = Field(description="The PR has moved past the head this review covers")
    nothing_to_walk: bool
    sync: SyncOut

    @classmethod
    def of(cls, view: PrView) -> PrOut:
        return cls(
            pr=QueueRowOut.of(view.row),
            round=None if view.round is None else RoundOut.of(view.round),
            artifacts=_json(view.artifacts),
            stale=view.stale,
            nothing_to_walk=view.nothing_to_walk,
            sync=SyncOut.of(view.sync),
        )


def _json(artifacts: Mapping[str, object]) -> dict[str, JsonValue]:
    # Artifacts are stored as JSON and decoded with json.loads: JSON values already.
    return cast(dict[str, JsonValue], dict(artifacts))


class OpenOut(BaseModel):
    """What opening did: ``started`` jobs (202), or the round's state, or the gate holding
    it (``ci_failing``…, with **Review anyway** offered)."""

    state: str
    reason: str
    round_id: int | None
    jobs: list[str]

    @classmethod
    def of(cls, result: OpenResult) -> OpenOut:
        return cls(
            state=result.state,
            reason=result.reason,
            round_id=result.round_id,
            jobs=[job.value for job in result.jobs],
        )


class DraftBody(BaseModel):
    """A draft review comment: inline (``line``, ``side`` and, for a range, ``start_line``
    and ``start_side``) or ``outside_diff`` (posted in the review body)."""

    id: int | None = None
    path: str = Field(min_length=1, max_length=PATH_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)
    line: int | None = Field(default=None, ge=1)
    side: ReviewSide | None = None
    start_line: int | None = Field(default=None, ge=1)
    start_side: ReviewSide | None = None
    origin_finding: str | None = Field(default=None, max_length=100)
    outdated: bool = False
    outside_diff: bool = False
    in_body: bool | None = Field(
        default=None,
        description="Read only: whether it posts in the review body (GitHub can't anchor it "
        "inline); null while GitHub's file list for the head isn't cached",
    )

    @classmethod
    def of(cls, draft: Draft, in_body: bool | None = None) -> DraftBody:
        return cls(
            id=draft.id,
            path=draft.path,
            body=draft.body,
            line=draft.line,
            side=draft.side,
            start_line=draft.start_line,
            start_side=draft.start_side,
            origin_finding=draft.origin_finding,
            outdated=draft.outdated,
            outside_diff=draft.outside_diff,
            in_body=in_body,
        )

    def draft(self) -> Draft:
        """Raises: InvalidDraftError: GitHub would reject it."""
        return Draft(
            path=self.path,
            body=self.body,
            line=self.line,
            side=self.side,
            start_line=self.start_line,
            start_side=self.start_side,
            origin_finding=self.origin_finding,
            outdated=self.outdated,
            outside_diff=self.outside_diff,
        )


class DraftsIn(BaseModel):
    summary: str = Field(default="", max_length=BODY_MAX, description="The review body")
    drafts: list[DraftBody] = Field(default_factory=list, max_length=DRAFTS_MAX)


class DraftsOut(BaseModel):
    round_id: int
    summary: str
    drafts: list[DraftBody]
    files_pending: bool = Field(
        description="GitHub's file list for the head isn't cached yet, so where each draft "
        "posts is unknown; the preview reads it"
    )

    @classmethod
    def of(cls, drafts: DraftSet) -> DraftsOut:
        placed = drafts.in_body or (None,) * len(drafts.drafts)
        return cls(
            round_id=drafts.round_id,
            summary=drafts.summary,
            drafts=[DraftBody.of(d, where) for d, where in zip(drafts.drafts, placed, strict=True)],
            files_pending=drafts.files_pending,
        )


class PreviewIn(BaseModel):
    event: ReviewEvent


class CommentOut(BaseModel):
    path: str
    line: int
    side: ReviewSide
    body: str
    start_line: int | None
    start_side: ReviewSide | None

    @classmethod
    def of(cls, comment: DraftComment) -> CommentOut:
        return cls(
            path=comment.path,
            line=comment.line,
            side=comment.side,
            body=comment.body,
            start_line=comment.start_line,
            start_side=comment.start_side,
        )


class PreviewOut(BaseModel):
    """Exactly what posting ``event`` sends; pass ``digest`` back to post it."""

    round_id: int
    head_sha: str
    event: ReviewEvent
    body: str
    comments: list[CommentOut]
    warnings: list[PostWarning]
    allowed_events: list[ReviewEvent]
    digest: str
    notes: list[str]

    @classmethod
    def of(cls, preview: PostPreview) -> PreviewOut:
        return cls(
            round_id=preview.round_id,
            head_sha=preview.head_sha,
            event=preview.event,
            body=preview.body,
            comments=[CommentOut.of(comment) for comment in preview.comments],
            warnings=list(preview.warnings),
            allowed_events=list(preview.allowed_events),
            digest=preview.digest,
            notes=list(preview.notes),
        )


class PostIn(BaseModel):
    event: ReviewEvent
    confirm: list[PostWarning] = Field(
        default_factory=list, max_length=3, description="Every warning the preview showed"
    )
    digest: str = Field(
        min_length=1, max_length=128, description="The digest of the preview being confirmed"
    )


class PostedOut(BaseModel):
    review_url: str


class ReviewRepoSettingBody(BaseModel):
    repo: str = Field(min_length=3, max_length=REPO_MAX)
    watched: bool
    suggested: bool = False
    prereview: PreReviewMode | None = None
    automated_reviewer: str | None = Field(default=None, min_length=1, max_length=REVIEWER_MAX)

    @classmethod
    def of(cls, config: RepoConfig) -> ReviewRepoSettingBody:
        return cls(
            repo=config.repo,
            watched=config.watched,
            suggested=config.suggested,
            prereview=config.prereview,
            automated_reviewer=config.automated_reviewer,
        )

    def config(self) -> RepoConfig:
        """Raises: InvalidPrRefError: ``repo`` isn't ``owner/name``."""
        return RepoConfig(
            self.repo,
            watched=self.watched,
            automated_reviewer=self.automated_reviewer,
            prereview=self.prereview,
            suggested=self.suggested,
        )


class ReviewSettingsBody(BaseModel):
    """Settings → Review."""

    prereview: PreReviewMode
    runner: str = Field(min_length=1, max_length=RUNNER_MAX, description="The agent runner")
    repos: list[ReviewRepoSettingBody] = Field(default_factory=list, max_length=500)

    @classmethod
    def of(cls, settings: ReviewSettings) -> ReviewSettingsBody:
        return cls(
            prereview=settings.prereview,
            runner=settings.runner,
            repos=[ReviewRepoSettingBody.of(config) for config in settings.repos],
        )


class ReviewErrorOut(BaseModel):
    error: str
    detail: str
    warnings: list[PostWarning] | None = Field(
        default=None, description="For NeedsConfirmation: every warning to confirm"
    )
    review_url: str | None = Field(
        default=None, description="For EarlierReviewPosted: the review GitHub already has"
    )
    event: ReviewEvent | None = Field(
        default=None, description="For EarlierReviewPosted: that review's event"
    )
    retry_after: int | None = Field(
        default=None,
        description="For PostingStillUncertain and PostingInProgress: seconds before "
        "retrying (also the Retry-After header)",
    )
