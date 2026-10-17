# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response bodies of ``/api/issues`` (the generated web client's types). Issue
titles, bodies, labels and comments are the issue authors' text: the PWA shows it as data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field, JsonValue

from jesinia.apps.issues.application.labels import LabelRun, OpResult
from jesinia.apps.issues.application.ports import (
    AppliedBatch,
    ApplyOrigin,
    RepoState,
    RunSource,
    RunStatus,
    SchemaSource,
    SuggestionRun,
)
from jesinia.apps.issues.application.reads import IssueDetail
from jesinia.apps.issues.application.suggest import MAX_BACKLOG_REFS
from jesinia.apps.issues.domain.changes import (
    AddArea,
    Close,
    Comment,
    CreateIssue,
    EnsureLabel,
    Existing,
    IssueChange,
    PendingChange,
    PendingSource,
    RemoveArea,
    ReorderChild,
    SetDimension,
    SetParent,
    SingleDimension,
    StepKind,
    StepResult,
    StepState,
    Target,
)
from jesinia.apps.issues.domain.labels import (
    CreateLabel,
    LabelOp,
    LabelPlan,
    LabelPlanKind,
    skip_cause,
)
from jesinia.apps.issues.domain.schema import (
    MAX_ALIASES,
    MAX_ENTRIES,
    MAX_LABEL_NAME,
    Canonical,
    LabelSchema,
)
from jesinia.apps.issues.domain.suggestions import Suggestion, SuggestionKind, SuggestionState
from jesinia.apps.issues.domain.triage import IssueRow, IssueView
from jesinia.services.github.domain.issue import (
    COMMENT_BODY_LIMIT,
    CloseReason,
    IssueComment,
    TimelineEvent,
)

REPO_MAX = 140
PLAN_ID_MAX = 64

LabelName = Field(min_length=1, max_length=MAX_LABEL_NAME)
LabelText = Annotated[str, Field(min_length=1, max_length=MAX_LABEL_NAME)]


class IssuesErrorOut(BaseModel):
    """A typed error: its class name, the cause, and (when GitHub rate limited the request)
    the seconds to wait, also sent as ``Retry-After``."""

    error: str
    detail: str
    retry_after: int | None = None


class LabelCanonicalBody(BaseModel):
    """A canonical label value and the existing labels that mean the same thing."""

    name: str = LabelName
    aliases: list[LabelText] = Field(default_factory=list, max_length=MAX_ALIASES)

    @classmethod
    def of(cls, canonical: Canonical) -> LabelCanonicalBody:
        return cls(name=canonical.name, aliases=list(canonical.aliases))


class LabelSchemaBody(BaseModel):
    """A label schema in ``.jesinia/labels.yml``'s shape (spec §2)."""

    readiness: list[LabelCanonicalBody] = Field(max_length=MAX_ENTRIES)
    priority: list[LabelCanonicalBody] = Field(max_length=MAX_ENTRIES)
    type: list[LabelCanonicalBody] = Field(max_length=MAX_ENTRIES)
    areas: list[LabelCanonicalBody] = Field(default_factory=list, max_length=MAX_ENTRIES)
    epic: LabelCanonicalBody = Field(default_factory=lambda: LabelCanonicalBody(name="epic"))
    initiative: LabelCanonicalBody = Field(
        default_factory=lambda: LabelCanonicalBody(name="initiative")
    )
    free_prefixes: list[LabelText] = Field(default_factory=list, max_length=MAX_ENTRIES)

    @classmethod
    def of(cls, schema: LabelSchema) -> LabelSchemaBody:
        def values(items: tuple[Canonical, ...]) -> list[LabelCanonicalBody]:
            return [LabelCanonicalBody.of(item) for item in items]

        return cls(
            readiness=values(schema.readiness),
            priority=values(schema.priority),
            type=values(schema.type),
            areas=values(schema.areas),
            epic=LabelCanonicalBody.of(schema.epic),
            initiative=LabelCanonicalBody.of(schema.initiative),
            free_prefixes=list(schema.free_prefixes),
        )


class IssuesRepoOut(BaseModel):
    """A repo the author added. ``label_schema`` is the effective one and ``schema_source`` where
    it came from: ``repo`` (its ``.jesinia/labels.yml``, which wins while it exists),
    ``settings`` (``settings_schema``) or ``default``. ``schema_errors`` lists what labels.yml
    couldn't use."""

    repo: str
    watched: bool
    added_at: datetime
    schema_source: SchemaSource
    label_schema: LabelSchemaBody
    schema_errors: list[str]
    settings_schema: LabelSchemaBody | None
    migrated: bool
    synced_at: datetime | None
    sync_error: str | None

    @classmethod
    def of(cls, state: RepoState) -> IssuesRepoOut:
        settings = state.settings_schema
        return cls(
            repo=state.repo,
            watched=state.watched,
            added_at=state.added_at,
            schema_source=state.schema_source,
            label_schema=LabelSchemaBody.of(state.schema),
            schema_errors=list(state.schema.errors),
            settings_schema=None if settings is None else LabelSchemaBody.of(settings),
            migrated=state.migrated,
            synced_at=state.synced_at,
            sync_error=state.sync_error,
        )


class IssuesReposOut(BaseModel):
    repos: list[IssuesRepoOut]


class IssuesRepoIn(BaseModel):
    """Settings → Issues for one repo. PUT replaces, so ``settings_schema`` is required: send
    the schema to keep, or an explicit ``null`` to clear it (the default then applies when there
    is no labels.yml). Leaving it out is a 422, never a silent wipe of the author's schema."""

    watched: bool
    settings_schema: LabelSchemaBody | None


class IssueRowOut(BaseModel):
    """An Inbox or Backlog row with its triage against the repo's schema."""

    repo: str
    number: int
    title: str
    url: str
    author: str
    labels: list[str]
    readiness: str | None
    priority: str | None
    type: str | None
    areas: list[str]
    complete: bool
    missing: list[str]
    conflicts: list[str]
    is_epic: bool
    sub_issue_count: int
    parent: str | None
    milestone: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: IssueRow) -> IssueRowOut:
        return cls(**vars(row))


class IssueViewOut(BaseModel):
    """A list served from the cache at once; the sync's state is reported, never awaited."""

    rows: list[IssueRowOut]
    synced_at: datetime | None
    syncing: bool
    sync_error: str | None

    @classmethod
    def of(cls, view: IssueView) -> IssueViewOut:
        return cls(
            rows=[IssueRowOut.of(row) for row in view.rows],
            synced_at=view.synced_at,
            syncing=view.syncing,
            sync_error=view.sync_error,
        )


class IssueCommentOut(BaseModel):
    id: int
    author: str
    body: str
    created_at: datetime
    url: str

    @classmethod
    def of(cls, comment: IssueComment) -> IssueCommentOut:
        return cls(**vars(comment))


class IssueEventOut(BaseModel):
    """A timeline event; ``source`` is the referencing issue or PR as ``owner/name#n``."""

    id: int | None
    event: str
    actor: str
    created_at: datetime | None
    label: str | None
    state_reason: str | None
    source: str | None
    commit_id: str | None

    @classmethod
    def of(cls, event: TimelineEvent) -> IssueEventOut:
        source = event.source
        return cls(
            id=event.id,
            event=event.event,
            actor=event.actor,
            created_at=event.created_at,
            label=event.label,
            state_reason=event.state_reason,
            source=None if source is None else f"{source.repo}#{source.number}",
            commit_id=event.commit_id,
        )


class IssueDetailOut(BaseModel):
    """One cached issue for the book layout. ``comments`` and ``timeline`` are what the last
    refresh fetched (``details_synced_at``; ``None``: never); ``children`` is ``None`` while an
    epic's sub-issues were never fetched."""

    repo: str
    number: int
    title: str
    body: str
    url: str
    author: str
    state: str
    state_reason: str | None
    labels: list[str]
    milestone: str | None
    parent: str | None
    created_at: datetime
    updated_at: datetime
    readiness: str | None
    priority: str | None
    type: str | None
    areas: list[str]
    complete: bool
    missing: list[str]
    conflicts: list[str]
    is_epic: bool
    inbox: bool
    sub_issue_count: int
    children: list[str] | None
    comments: list[IssueCommentOut]
    timeline: list[IssueEventOut]
    details_synced_at: datetime | None

    @classmethod
    def of(cls, detail: IssueDetail) -> IssueDetailOut:
        issue, triage = detail.cached.issue, detail.cached.triage
        parent = issue.parent
        return cls(
            repo=issue.repo,
            number=issue.number,
            title=issue.title,
            body=issue.body,
            url=issue.url,
            author=issue.author,
            state=issue.state,
            state_reason=issue.state_reason,
            labels=list(issue.labels),
            milestone=issue.milestone,
            parent=None if parent is None else f"{parent.repo}#{parent.number}",
            created_at=issue.created_at,
            updated_at=issue.updated_at,
            readiness=triage.readiness,
            priority=triage.priority,
            type=triage.type,
            areas=triage.areas,
            complete=triage.complete,
            missing=triage.missing,
            conflicts=triage.conflicts,
            is_epic=triage.is_epic,
            inbox=detail.cached.inbox and not triage.complete,
            sub_issue_count=issue.sub_issue_count,
            children=None
            if detail.children is None
            else [f"{child.repo}#{child.number}" for child in detail.children],
            comments=[IssueCommentOut.of(comment) for comment in detail.comments],
            timeline=[IssueEventOut.of(event) for event in detail.timeline],
            details_synced_at=detail.cached.details_synced_at,
        )


class LabelPlanIn(BaseModel):
    kind: LabelPlanKind


class LabelApplyIn(BaseModel):
    """Apply a previewed plan (an approval). ``plan_id`` is the preview's."""

    kind: LabelPlanKind
    plan_id: str = Field(min_length=1, max_length=PLAN_ID_MAX)


class LabelOpOut(BaseModel):
    """One step: ``create`` (``name``, ``color``, ``description``) or ``delete`` (``name``).
    A delete with a ``replacement`` first moves every issue and pull request carrying the label
    to it (listed live at apply time); ``members`` is what the preview listed. ``skip_cause``
    says why the step will be skipped whatever happens (a name GitHub can't list by)."""

    op: Literal["create", "delete"]
    name: str
    color: str | None
    description: str | None
    replacement: str | None
    members: list[int]
    skip_cause: str | None

    @classmethod
    def of(cls, op: LabelOp) -> LabelOpOut:
        if isinstance(op, CreateLabel):
            return cls(
                op="create",
                name=op.name,
                color=op.color,
                description=op.description,
                replacement=None,
                members=[],
                skip_cause=None,
            )
        return cls(
            op="delete",
            name=op.name,
            color=None,
            description=None,
            replacement=op.replacement,
            members=list(op.members),
            skip_cause=skip_cause(op),
        )


class LabelPlanOut(BaseModel):
    """A previewed batch: apply it with ``plan_id`` before ``expires_at``."""

    plan_id: str
    repo: str
    kind: LabelPlanKind
    expires_at: datetime
    ops: list[LabelOpOut]

    @classmethod
    def of(cls, plan: LabelPlan) -> LabelPlanOut:
        return cls(
            plan_id=plan.id,
            repo=plan.repo,
            kind=plan.kind,
            expires_at=plan.expires_at,
            ops=[LabelOpOut.of(op) for op in plan.ops],
        )


class LabelStepOut(BaseModel):
    """A step's outcome; ``failed_issues`` are the issues a delete's moves failed on."""

    op: LabelOpOut
    state: Literal["done", "failed", "skipped"]
    cause: str | None
    failed_issues: list[int]

    @classmethod
    def of(cls, result: OpResult) -> LabelStepOut:
        return cls(
            op=LabelOpOut.of(result.op),
            state=result.state,
            cause=result.cause,
            failed_issues=list(result.failed_issues),
        )


class LabelApplyOut(BaseModel):
    """What an apply did, step by step. ``note`` says why old labels were kept;
    ``stopped`` is set when GitHub stopped the batch (a rate limit, with
    ``rate_limited_until`` and ``Retry-After``, or a rejected token): the response is then a 503.
    Anything left needs a fresh preview."""

    repo: str
    kind: LabelPlanKind
    complete: bool
    steps: list[LabelStepOut]
    note: str | None
    stopped: str | None
    rate_limited: bool
    rate_limited_until: datetime | None

    @classmethod
    def of(cls, repo: str, kind: LabelPlanKind, run: LabelRun) -> LabelApplyOut:
        return cls(
            repo=repo,
            kind=kind,
            complete=run.complete,
            steps=[LabelStepOut.of(result) for result in run.results],
            note=run.note,
            stopped=run.stopped,
            rate_limited=run.is_rate_limited,
            rate_limited_until=run.rate_limited_until,
        )


# Suggestions (Task 5) ------------------------------------------------------------------------


class IssueSuggestionOut(BaseModel):
    """A suggestion draft. ``repo`` and ``n`` name its issue (``scope`` a draft about several
    issues or a queue, from later tasks). ``payload`` is Jesinia's own JSON, by ``kind``:

    - ``label`` and ``new_area``: ``{dimension, value}`` (``new_area``: an area not in the
      schema yet, which approving adds);
    - ``duplicate``: ``{repo, n, similarity}``; ``parent``: ``{repo, n}`` (the epic);
    - ``readiness_gap``: ``{ready, missing}`` (``ready``: a ``human-needed`` issue could be
      ``agent-ready``; else what ``agent-ready`` still lacks);
    - ``priority`` (about a batch, so ``scope``): ``{items: [{repo, n, priority, current, rank,
      reason}]}`` in rank order; ``priority`` is the schema value to set (``null``: none), and
      ``current`` the priority the issue had, which approving applies against.

    ``reason`` is the agent's one line, untrusted text shown as data. ``apply_error`` is why
    its last approval didn't apply (it stays approvable)."""

    id: str
    repo: str | None
    n: int | None
    scope: str | None
    kind: SuggestionKind
    payload: dict[str, JsonValue]
    reason: str
    state: SuggestionState
    created_at: datetime
    updated_at: datetime
    apply_error: str | None = None

    @classmethod
    def of(cls, suggestion: Suggestion) -> IssueSuggestionOut:
        return cls(
            id=suggestion.id,
            repo=suggestion.repo,
            n=suggestion.n,
            scope=suggestion.scope,
            kind=suggestion.kind,
            payload=cast(dict[str, JsonValue], dict(suggestion.payload)),
            reason=suggestion.reason,
            state=suggestion.state,
            created_at=suggestion.created_at,
            updated_at=suggestion.updated_at,
            apply_error=suggestion.apply_error,
        )


class IssueSuggestionRunOut(BaseModel):
    """An issue's latest suggestion run: ``queued`` (until ``due_at``), ``running``, ``done``,
    ``skipped`` (it left the Inbox first), ``waiting`` (no runner; manual triage works) or
    ``failed`` (``cause``). ``source``: ``inbox`` (background) or ``demand``."""

    repo: str
    n: int
    status: RunStatus
    source: RunSource
    due_at: datetime
    cause: str | None
    updated_at: datetime

    @classmethod
    def of(cls, run: SuggestionRun) -> IssueSuggestionRunOut:
        return cls(**vars(run))


class IssueSuggestionsOut(BaseModel):
    """Suggestions, oldest first; ``run`` is the issue's latest run when one issue was asked
    for."""

    suggestions: list[IssueSuggestionOut]
    run: IssueSuggestionRunOut | None


class IssueSuggestionEditIn(BaseModel):
    """The author's edit: the whole new ``payload`` for the suggestion's kind (see
    ``IssueSuggestionOut``), and optionally a new ``reason``."""

    payload: dict[str, JsonValue] = Field(max_length=8)
    reason: str | None = Field(default=None, max_length=1000)


class IssueRefIn(BaseModel):
    repo: str = Field(min_length=3, max_length=REPO_MAX)
    n: int = Field(ge=1, le=999_999_999)


class IssueSuggestionRunIn(BaseModel):
    """Issues to suggest for now (usually from the Backlog, which never gets suggestions in the
    background)."""

    refs: list[IssueRefIn] = Field(min_length=1, max_length=MAX_BACKLOG_REFS)


class IssueSuggestionRunsOut(BaseModel):
    runs: list[IssueSuggestionRunOut]


# The apply engine and the pending tray (Task 6) ----------------------------------------------


class IssueTargetBody(BaseModel):
    """An existing issue."""

    repo: str = Field(min_length=3, max_length=REPO_MAX)
    n: int = Field(ge=1, le=999_999_999)


class SetDimensionBody(BaseModel):
    """Set readiness, priority or type to a schema value (its other values are removed)."""

    kind: Literal["set_dimension"]
    dimension: SingleDimension
    value: LabelText


class AddAreaBody(BaseModel):
    kind: Literal["add_area"]
    value: LabelText


class RemoveAreaBody(BaseModel):
    kind: Literal["remove_area"]
    value: LabelText


class SetParentBody(BaseModel):
    """Make ``parent`` the issue's epic, or remove its parent (``null``). In the tray,
    ``expects_parent`` and ``expected_parent`` show the parent it was staged against (a move on
    GitHub since is a conflict). Staging records them from the cache and ignores what is sent."""

    kind: Literal["set_parent"]
    parent: IssueTargetBody | None
    expects_parent: bool = False
    expected_parent: IssueTargetBody | None = None


class ReorderChildBody(BaseModel):
    """Within this epic, move ``child`` just after ``after`` (the top when ``null``)."""

    kind: Literal["reorder_child"]
    child: IssueTargetBody
    after: IssueTargetBody | None = None


class CloseBody(BaseModel):
    """Close the issue; a duplicate names its original (which must be cached) and may carry a
    comment, posted after the close."""

    kind: Literal["close"]
    reason: CloseReason
    duplicate_of: IssueTargetBody | None = None
    comment: str | None = Field(default=None, max_length=COMMENT_BODY_LIMIT)


class CommentBody(BaseModel):
    kind: Literal["comment"]
    body: str = Field(min_length=1, max_length=COMMENT_BODY_LIMIT)


IssueChangeBody = Annotated[
    SetDimensionBody
    | AddAreaBody
    | RemoveAreaBody
    | SetParentBody
    | ReorderChildBody
    | CloseBody
    | CommentBody,
    Field(discriminator="kind"),
]


def _target_body(target: Target | None) -> IssueTargetBody | None:
    """A staged change's issue: always an existing one (the tray holds no ``CreateIssue``, so
    staging refuses a new issue's temp id)."""
    if target is None:
        return None
    existing = cast(Existing, target)
    return IssueTargetBody(repo=existing.repo, n=existing.n)


def change_body(change: IssueChange) -> IssueChangeBody:
    """A staged change as the API shows it."""
    if isinstance(change, SetDimension):
        return SetDimensionBody(
            kind="set_dimension", dimension=change.dimension, value=change.value
        )
    if isinstance(change, AddArea):
        return AddAreaBody(kind="add_area", value=change.value)
    if isinstance(change, RemoveArea):
        return RemoveAreaBody(kind="remove_area", value=change.value)
    if isinstance(change, SetParent):
        return SetParentBody(
            kind="set_parent",
            parent=_target_body(change.parent),
            expects_parent=change.expects_parent,
            expected_parent=_target_body(change.expected_parent),
        )
    if isinstance(change, ReorderChild):
        child = cast(IssueTargetBody, _target_body(change.child))
        return ReorderChildBody(kind="reorder_child", child=child, after=_target_body(change.after))
    if isinstance(change, Close):
        return CloseBody(
            kind="close",
            reason=change.reason,
            duplicate_of=_target_body(change.duplicate_of_ref),
            comment=change.comment,
        )
    return CommentBody(kind="comment", body=change.body)


def body_change(
    body: IssueChangeBody, original: Callable[[str, int], tuple[Existing, int]]
) -> IssueChange:
    """A staged change from its body; ``original`` finds a duplicate's original and its GitHub
    id in the cache (``DuplicateNotCachedError`` when it isn't there)."""

    def target(found: IssueTargetBody) -> Existing:
        return Existing(found.repo, found.n)

    if isinstance(body, SetDimensionBody):
        return SetDimension(body.dimension, body.value)
    if isinstance(body, AddAreaBody):
        return AddArea(body.value)
    if isinstance(body, RemoveAreaBody):
        return RemoveArea(body.value)
    if isinstance(body, SetParentBody):
        return SetParent(None if body.parent is None else target(body.parent))
    if isinstance(body, ReorderChildBody):
        after = None if body.after is None else target(body.after)
        return ReorderChild(target(body.child), after)
    if isinstance(body, CloseBody):
        if body.duplicate_of is None:
            return Close(body.reason, comment=body.comment)
        ref, database_id = original(body.duplicate_of.repo, body.duplicate_of.n)
        return Close(body.reason, database_id, ref, body.comment)
    return Comment(body.body)


class PendingIn(BaseModel):
    """A change to stage for a cached issue, and what staged it."""

    repo: str = Field(min_length=3, max_length=REPO_MAX)
    n: int = Field(ge=1, le=999_999_999)
    change: IssueChangeBody
    source: PendingSource


class PendingOut(BaseModel):
    id: str
    repo: str
    n: int
    change: IssueChangeBody
    source: PendingSource
    created_at: datetime

    @classmethod
    def of(cls, pending: PendingChange) -> PendingOut:
        return cls(
            id=pending.id,
            repo=pending.target.repo,
            n=pending.target.n,
            change=change_body(pending.change),
            source=pending.source,
            created_at=pending.created_at,
        )


class PendingListOut(BaseModel):
    """The tray, in the order staged."""

    changes: list[PendingOut]


class PendingApplyIn(BaseModel):
    """Which staged changes to apply (all of them when ``ids`` is omitted or ``null``)."""

    ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=1000
    )


class ApplyStepOut(BaseModel):
    """One step: ``done`` (``cause`` may say it was already so), ``failed`` or ``skipped``
    (``cause`` says why). ``conflict``: it failed because GitHub changed underneath it (a parent
    moved, a dimension filled, the issue closed), shown as a conflict, not overwritten."""

    kind: StepKind
    state: StepState
    cause: str | None
    conflict: bool = False

    @classmethod
    def of(cls, result: StepResult) -> ApplyStepOut:
        return cls(
            kind=result.step.kind,
            state=result.state,
            cause=result.cause,
            conflict=result.is_conflict,
        )


class ApplyIssueOut(BaseModel):
    """One issue's steps in order: ``repo`` and ``n`` for an existing issue, ``temp_id`` for one
    the batch creates (``repo`` and ``n`` filled once it exists)."""

    repo: str | None
    n: int | None
    temp_id: str | None
    steps: list[ApplyStepOut]


class ApplyRepoStepOut(ApplyStepOut):
    """A repo-level step: a label ensured (``label``) or an issue created (``temp_id``)."""

    repo: str
    label: str | None
    temp_id: str | None


class ApplyCreatedOut(BaseModel):
    temp_id: str
    repo: str
    n: int


class ApplyBatchOut(BaseModel):
    """An applied batch and its result. ``stopped`` is set when GitHub stopped it (a rate limit,
    with ``rate_limited_until`` and ``Retry-After``, or a rejected token): the answer is then a
    503 whose ``Location`` is its retry route, ``POST /pending/{id}/retry`` (the only way to
    resume it: its changes already left the tray)."""

    id: str
    origin: ApplyOrigin
    complete: bool
    repo_steps: list[ApplyRepoStepOut]
    issues: list[ApplyIssueOut]
    created: list[ApplyCreatedOut]
    stopped: str | None
    rate_limited: bool
    rate_limited_until: datetime | None

    @classmethod
    def of(cls, applied: AppliedBatch) -> ApplyBatchOut:
        result = applied.result
        issues = []
        for target, found in result.per_issue.items():
            if isinstance(target, Existing):
                where: tuple[str | None, int | None, str | None] = (target.repo, target.n, None)
            else:
                made = result.created.get(target.temp_id)
                where = (
                    None if made is None else made.repo,
                    None if made is None else made.n,
                    target.temp_id,
                )
            issues.append(
                ApplyIssueOut(
                    repo=where[0],
                    n=where[1],
                    temp_id=where[2],
                    steps=[ApplyStepOut.of(step) for step in found.steps],
                )
            )
        repo_steps = []
        for step in result.repo_steps:
            change = step.step.changes[0]
            repo_steps.append(
                ApplyRepoStepOut(
                    kind=step.step.kind,
                    state=step.state,
                    cause=step.cause,
                    repo=step.step.repo,
                    label=change.name if isinstance(change, EnsureLabel) else None,
                    temp_id=change.temp_id if isinstance(change, CreateIssue) else None,
                )
            )
        return cls(
            id=applied.id,
            origin=applied.origin,
            complete=result.complete,
            repo_steps=repo_steps,
            issues=issues,
            created=[
                ApplyCreatedOut(temp_id=temp, repo=made.repo, n=made.n)
                for temp, made in result.created.items()
            ],
            stopped=result.stopped,
            rate_limited=result.is_rate_limited,
            rate_limited_until=result.rate_limited_until,
        )


class SuggestionApproveOut(BaseModel):
    """The suggestion after its approval (``approved``, or still open with ``apply_error``) and
    the batch that applied it."""

    suggestion: IssueSuggestionOut
    batch: ApplyBatchOut
