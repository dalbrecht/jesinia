# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response bodies of ``/api/review`` (the generated web client's types)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool

from jesinia.apps.review.application.knowledge import KnowledgeView, UnitView
from jesinia.apps.review.application.pipeline import OpenResult
from jesinia.apps.review.application.posting import DraftSet, PostPreview
from jesinia.apps.review.application.reviews import PrView, ReviewSettings
from jesinia.apps.review.application.stacks import StackOpenResult, StackView
from jesinia.apps.review.domain.followup import FollowUpKind
from jesinia.apps.review.domain.gates import CiState, Depth
from jesinia.apps.review.domain.kept import KeptState
from jesinia.apps.review.domain.knowledge import (
    QUIZ_MAX_QUESTIONS,
    ExposureKind,
    Override,
    UnitKind,
)
from jesinia.apps.review.domain.metrics import Point
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
from jesinia.apps.review.domain.stacks import (
    STACK_MAX,
    CrossFinding,
    PostAllItem,
    PostOutcome,
    PostState,
)
from jesinia.services.github.domain.pr import DraftComment, PrFile, ReviewEvent, ReviewSide
from jesinia.services.kb.application.service import PageWithRev

REF_MAX = 500
REPO_MAX = 140
RUNNER_MAX = 64
REVIEWER_MAX = 100
# Generous bounds on what a request may carry; the domain checks the real limits (400).
METRICS_MAX_SENT = 200
METRIC_MAX_SENT = 1000
FOLDER_MAX_SENT = 4096


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
    followup_kind: FollowUpKind | None = Field(
        default=None,
        description="What this head needs after the author's previous review (spec §4.1): "
        "still_approved, changed_hunks_only, rereview or new; null until classified",
    )
    followup_note: str = Field(default="", description="One line on it, in Jesinia's words")
    differing_paths: list[str] = Field(
        default_factory=list,
        description="Files whose changes differ from the previous review's (PR text)",
    )
    stack: list[int] = Field(
        default_factory=list,
        description="The PR's stack (spec §4.3) as PR numbers, bottom-up; empty if none",
    )
    recommendation_label: str | None = Field(
        default=None,
        description="The recommendation in words; on your own PR, 'ready to request review' "
        "or 'not ready to request review' (spec §3.3)",
    )

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
            followup_kind=row.followup_kind,
            followup_note=row.followup_note,
            differing_paths=list(row.differing_paths),
            stack=list(row.stack),
            recommendation_label=row.recommendation_label,
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
    frozen: bool = Field(
        default=False,
        description="Read-only: a round on the PR's new head took over its drafts",
    )
    successor_id: int | None = None

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
            frozen=rnd.frozen_at is not None,
            successor_id=rnd.successor_id,
        )


class PrFileOut(BaseModel):
    """A changed file as GitHub lists it; ``patch`` (the file's unified diff hunks, PR
    content) is null when GitHub doesn't show it (binary, or too large)."""

    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_path: str | None

    @classmethod
    def of(cls, file: PrFile) -> PrFileOut:
        return cls(
            path=file.filename,
            status=file.status,
            additions=file.additions,
            deletions=file.deletions,
            patch=file.patch,
            previous_path=file.previous_filename,
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
    files: list[PrFileOut] | None = Field(
        description="GitHub's cached files of the round's head, for the walkthrough's diff; "
        "null before any round or while that head's list isn't cached"
    )

    @classmethod
    def of(cls, view: PrView) -> PrOut:
        return cls(
            pr=QueueRowOut.of(view.row),
            round=None if view.round is None else RoundOut.of(view.round),
            artifacts=_json(view.artifacts),
            stale=view.stale,
            nothing_to_walk=view.nothing_to_walk,
            sync=SyncOut.of(view.sync),
            files=None if view.files is None else [PrFileOut.of(f) for f in view.files],
        )


def _json(artifacts: Mapping[str, object]) -> dict[str, JsonValue]:
    # Artifacts are stored as JSON and decoded with json.loads: JSON values already.
    return cast(dict[str, JsonValue], dict(artifacts))


class DoneIn(BaseModel):
    """Optional: the round the author was shown (the PWA always sends it)."""

    model_config = ConfigDict(extra="forbid")

    round_id: int | None = None


class OpenIn(BaseModel):
    """What the author asks of opening (both optional): their ``depth`` in place of
    Jesinia's proposal, or ``walk_everything`` to walk the rest of the change too."""

    model_config = ConfigDict(extra="forbid")

    depth: Depth | None = None
    walk_everything: StrictBool = False
    quiz: StrictBool = Field(
        default=False, description="Generate the quiz, which is off on your own PR (spec §3.3)"
    )


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
            id=self.id,
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
    round_id: int | None = Field(
        default=None,
        description="The round the author was shown; refused with 409 RoundChanged once a "
        "newer round is current (the PWA always sends it)",
    )


class DraftsOut(BaseModel):
    round_id: int
    summary: str
    drafts: list[DraftBody]
    files_pending: bool = Field(
        description="GitHub's file list for the head isn't cached yet, so where each draft "
        "posts is unknown; the preview reads it"
    )
    findings_pending: bool = Field(
        default=False,
        description="the correctness pass or the findings job is still running; their "
        "drafts are added (next to the author's) when both have finished",
    )
    seeds_dropped: int = Field(
        default=0,
        description="Finding drafts this save couldn't keep: a review takes at most "
        f"{DRAFTS_MAX} drafts, and the author's come first",
    )

    @classmethod
    def of(cls, drafts: DraftSet) -> DraftsOut:
        placed = drafts.in_body or (None,) * len(drafts.drafts)
        return cls(
            round_id=drafts.round_id,
            summary=drafts.summary,
            drafts=[DraftBody.of(d, where) for d, where in zip(drafts.drafts, placed, strict=True)],
            files_pending=drafts.files_pending,
            findings_pending=drafts.findings_pending,
            seeds_dropped=drafts.seeds_dropped,
        )


class PreviewIn(BaseModel):
    event: ReviewEvent
    round_id: int | None = Field(
        default=None,
        description="The round the author was shown; refused with 409 RoundChanged once a "
        "newer round is current (the PWA always sends it)",
    )


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
    pr_head_sha: str = Field(description="The PR's head on GitHub now (newer_head names it)")

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
            pr_head_sha=preview.pr_head_sha,
        )


class PostIn(BaseModel):
    event: ReviewEvent
    confirm: list[PostWarning] = Field(
        default_factory=list, max_length=3, description="Every warning the preview showed"
    )
    digest: str = Field(
        min_length=1, max_length=128, description="The digest of the preview being confirmed"
    )
    round_id: int | None = Field(
        default=None,
        description="The round the author was shown; refused with 409 RoundChanged once a "
        "newer round is current (the PWA always sends it)",
    )


class PostedOut(BaseModel):
    review_url: str


class ReviewRepoSettingBody(BaseModel):
    repo: str = Field(min_length=3, max_length=REPO_MAX)
    watched: bool
    suggested: bool = False
    prereview: PreReviewMode | None = None
    automated_reviewer: str | None = Field(default=None, min_length=1, max_length=REVIEWER_MAX)
    metrics: list[Annotated[str, Field(max_length=METRIC_MAX_SENT)]] | None = Field(
        default=None,
        max_length=METRICS_MAX_SENT,
        description="The criteria this repo's reviews are scored on, from the next round on; "
        "null or empty: the standards decide. Left out of a PUT: kept",
    )

    @classmethod
    def of(cls, config: RepoConfig) -> ReviewRepoSettingBody:
        return cls(
            repo=config.repo,
            watched=config.watched,
            suggested=config.suggested,
            prereview=config.prereview,
            automated_reviewer=config.automated_reviewer,
            metrics=None if config.metrics is None else list(config.metrics),
        )

    def config(self, current: RepoConfig | None = None) -> RepoConfig:
        """The repo's settings, its metrics kept from ``current`` when left out.

        Raises:
            InvalidPrRefError: ``repo`` isn't ``owner/name``.
            InvalidSettingError: a metric that isn't one short line of visible text.
        """
        sent = "metrics" in self.model_fields_set
        metrics = self.metrics if sent else (None if current is None else current.metrics)
        return RepoConfig(
            self.repo,
            watched=self.watched,
            automated_reviewer=self.automated_reviewer,
            prereview=self.prereview,
            suggested=self.suggested,
            metrics=None if metrics is None else tuple(metrics),
        )


class ReviewSettingsBody(BaseModel):
    """Settings → Review. A folder left out of a PUT keeps its value; null or "" clears it."""

    prereview: PreReviewMode
    runner: str = Field(min_length=1, max_length=RUNNER_MAX, description="The agent runner")
    repos: list[ReviewRepoSettingBody] = Field(default_factory=list, max_length=500)
    standards_dir: str | None = Field(
        default=None,
        max_length=FOLDER_MAX_SENT,
        description="The coding-standards checkout (the base layer): absolute, or ~/…",
    )
    standards_user_dir: str | None = Field(
        default=None,
        max_length=FOLDER_MAX_SENT,
        description="The folder of your own standards documents (the user layer)",
    )

    @classmethod
    def of(cls, settings: ReviewSettings) -> ReviewSettingsBody:
        return cls(
            prereview=settings.prereview,
            runner=settings.runner,
            repos=[ReviewRepoSettingBody.of(config) for config in settings.repos],
            standards_dir=None if settings.standards_dir is None else str(settings.standards_dir),
            standards_user_dir=None
            if settings.standards_user_dir is None
            else str(settings.standards_user_dir),
        )


class ScoreOut(BaseModel):
    """One criterion's score (agent text: render ``criterion`` as text)."""

    criterion: str
    score: float = Field(ge=0.0, le=10.0)


class PointOut(BaseModel):
    """One scored round of the repo; ``mean`` is its overall score from 0 to 10."""

    round_id: int
    number: int
    head_sha: str
    at: datetime
    mean: float = Field(ge=0.0, le=10.0)
    scores: list[ScoreOut]

    @classmethod
    def of(cls, point: Point) -> PointOut:
        return cls(
            round_id=point.round_id,
            number=point.number,
            head_sha=point.head_sha,
            at=point.at,
            mean=round(point.mean, 2),
            scores=[ScoreOut(criterion=s.criterion, score=s.score) for s in point.scores],
        )


class MetricsHistoryOut(BaseModel):
    """A repo's metrics over time (spec §5.2): its latest scored rounds, oldest first."""

    repo: str
    points: list[PointOut]


class CrossFindingOut(BaseModel):
    """A finding concerning more than one PR of a stack (agent text: render as text)."""

    prs: list[int]
    group: str
    severity: str
    explanation: str
    source: str = Field(
        description="shared_file: a PR's finding on a file another PR also changes; agent: "
        "reported across PRs by the stack walkthrough"
    )
    found_in: int | None
    path: str | None
    start_line: int | None
    end_line: int | None
    note: str

    @classmethod
    def of(cls, finding: CrossFinding) -> CrossFindingOut:
        return cls(
            prs=list(finding.prs),
            group=finding.group,
            severity=finding.severity,
            explanation=finding.explanation,
            source=finding.source,
            found_in=finding.found_in,
            path=finding.path,
            start_line=finding.start_line,
            end_line=finding.end_line,
            note=finding.note,
        )


class StackOut(BaseModel):
    """A stack from the cache (spec §4.3): each PR's own view bottom-up, the stack review of
    their current heads and its artifacts (overview, background, stack_walkthrough, jobs),
    and the cross-PR findings."""

    repo: str
    signature: str
    members: list[PrOut]
    stack_id: int | None
    artifacts: dict[str, JsonValue] = Field(
        description="The stack review's artifacts by kind (agent output: render as text)"
    )
    cross_findings: list[CrossFindingOut]

    @classmethod
    def of(cls, view: StackView) -> StackOut:
        return cls(
            repo=view.repo,
            signature=view.signature,
            members=[PrOut.of(member) for member in view.members],
            stack_id=view.stack_id,
            artifacts=_json(view.artifacts),
            cross_findings=[CrossFindingOut.of(f) for f in view.cross_findings],
        )


class StackOpenIn(BaseModel):
    """``retry``: run again the stack's jobs that failed."""

    model_config = ConfigDict(extra="forbid")

    retry: StrictBool = False


class StackMemberOpenOut(BaseModel):
    number: int
    state: str
    reason: str


class StackOpenOut(BaseModel):
    """What opening the stack did: each PR's open, and the stack's jobs (``held`` while a PR
    is held by a gate)."""

    state: str
    reason: str
    stack_id: int
    jobs: list[str]
    members: list[StackMemberOpenOut]

    @classmethod
    def of(cls, result: StackOpenResult) -> StackOpenOut:
        return cls(
            state=result.state,
            reason=result.reason,
            stack_id=result.stack_id,
            jobs=[job.value for job in result.jobs],
            members=[
                StackMemberOpenOut(number=number, state=opened.state, reason=opened.reason)
                for number, opened in result.members
            ],
        )


class PostAllItemIn(BaseModel):
    """One PR's confirmed review: its round, event, preview digest and confirmed warnings."""

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1, le=999_999_999)
    round_id: int = Field(ge=1)
    event: ReviewEvent
    digest: str = Field(min_length=1, max_length=128)
    confirm: list[PostWarning] = Field(default_factory=list, max_length=4)

    def item(self) -> PostAllItem:
        return PostAllItem(self.number, self.round_id, self.event, self.digest, tuple(self.confirm))


class PostAllIn(BaseModel):
    """Post all (spec §4.3): every PR's confirmed review, posted bottom-up."""

    model_config = ConfigDict(extra="forbid")

    items: list[PostAllItemIn] = Field(min_length=1, max_length=STACK_MAX)


class PostOutcomeOut(BaseModel):
    number: int
    round_id: int
    event: ReviewEvent
    state: PostState
    review_url: str | None
    error: str | None
    error_code: str | None

    @classmethod
    def of(cls, outcome: PostOutcome) -> PostOutcomeOut:
        return cls(
            number=outcome.number,
            round_id=outcome.round_id,
            event=outcome.event,
            state=outcome.state,
            review_url=outcome.review_url,
            error=outcome.error,
            error_code=outcome.error_code,
        )


class PostAllOut(BaseModel):
    outcomes: list[PostOutcomeOut]


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
    draft_index: int | None = Field(
        default=None, description="For InvalidDraftContent: the refused draft (0-based)"
    )
    field: str | None = Field(
        default=None, description="For InvalidDraftContent: 'summary' or 'draft'"
    )
    successor_round_id: int | None = Field(
        default=None, description="For RoundFrozen: the round that took over its drafts"
    )
    retry_after: int | None = Field(
        default=None,
        description="For PostingStillUncertain and PostingInProgress: seconds before "
        "retrying (also the Retry-After header)",
    )
    stack_outcomes: list[PostOutcomeOut] | None = Field(
        default=None,
        description="For a Post all that stopped: what was and wasn't posted, per PR (the "
        "error is the PR's that stopped it)",
    )


# --- the knowledge model (plan Task 14) ---------------------------------------------------------


class UnitOut(BaseModel):
    """One unit of the author's knowledge, as Settings → Review shows it."""

    unit: str = Field(description="repo/<owner>/<name>[/<path>] or tag/<tag>")
    kind: UnitKind
    repo: str | None
    path: str
    tag: str | None
    familiarity: float = Field(ge=0.0, le=1.0)
    override: Override | None = Field(
        description="The override in force: the unit's own, or its nearest folder's"
    )
    override_unit: str | None = Field(default=None, description="Where the override is set")
    exposures: dict[ExposureKind, int]
    last_exposure: datetime | None
    changes_since_last: int
    distance: float

    @classmethod
    def of(cls, view: UnitView) -> UnitOut:
        unit = view.unit
        return cls(
            unit=unit.id,
            kind=unit.kind,
            repo=unit.repo,
            path=unit.path,
            tag=unit.tag,
            familiarity=round(view.familiarity, 4),
            override=view.override,
            override_unit=None if view.override_unit is None else view.override_unit.id,
            exposures=dict(view.exposures),
            last_exposure=view.last_exposure,
            changes_since_last=view.changes_since_last,
            distance=round(view.distance, 4),
        )


class KnowledgeOut(BaseModel):
    """What Jesinia thinks you know: units by id, at most ``VIEW_LIMIT`` of ``total``."""

    units: list[UnitOut]
    total: int
    truncated: bool

    @classmethod
    def of(cls, view: KnowledgeView) -> KnowledgeOut:
        return cls(
            units=[UnitOut.of(u) for u in view.units], total=view.total, truncated=view.truncated
        )


class OverrideIn(BaseModel):
    """Mark a unit known or unknown; ``null`` clears the correction."""

    model_config = ConfigDict(extra="forbid")

    override: Override | None


class QuizIn(BaseModel):
    """A finished quiz: how many of the answered questions were right (skips don't count)."""

    model_config = ConfigDict(extra="forbid")

    round_id: int = Field(ge=1)
    right: int = Field(ge=0, le=QUIZ_MAX_QUESTIONS)
    answered: int = Field(ge=1, le=QUIZ_MAX_QUESTIONS)


class QuizOut(BaseModel):
    recorded: bool = Field(description="False when the round already had a result")


class KeepReviewIn(BaseModel):
    """Keep a round's review page again (Retry); the PR's latest round when omitted."""

    model_config = ConfigDict(extra="forbid")

    round_id: int | None = Field(default=None, ge=1)


class KeptReviewOut(BaseModel):
    """Where a round's review page went in the KB, or why it couldn't be kept."""

    state: Literal["kept", "failed"]
    page_id: str | None
    path: str | None = Field(description="The page's KB path (it carries the PR's title)")
    error: str | None = Field(description="Why the page couldn't be written (the cause)")

    @classmethod
    def of(cls, kept: KeptState) -> KeptReviewOut:
        return cls(state=kept.state, page_id=kept.page_id, path=kept.path, error=kept.error)


class SystemAcceptIn(BaseModel):
    """Accept the round's drafted How it works. ``base_rev`` is the page revision of the
    draft the author was shown (null for a seed); another one answers 409."""

    model_config = ConfigDict(extra="forbid")

    round_id: int = Field(ge=1)
    base_rev: str | None = Field(default=None, max_length=64)


class SystemPageOut(BaseModel):
    """The How it works page as written."""

    page_id: str
    path: str
    rev: str

    @classmethod
    def of(cls, page: PageWithRev) -> SystemPageOut:
        return cls(page_id=page.page.id, path=str(page.page.path), rev=page.rev)
