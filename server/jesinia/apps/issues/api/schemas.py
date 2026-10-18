# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response bodies of ``/api/issues`` (the generated web client's types). Issue
titles, bodies, labels and comments are the issue authors' text: the PWA shows it as data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field, JsonValue, model_validator

from jesinia.apps.issues.application.epics import (
    Containment,
    EpicChild,
    EpicDetail,
    EpicSummary,
    MilestoneItem,
)
from jesinia.apps.issues.application.labels import LabelRun, OpResult
from jesinia.apps.issues.application.ports import (
    AppliedBatch,
    ApplyOrigin,
    BriefState,
    CachedIssue,
    CreationState,
    OriginBatch,
    RepoState,
    RunSource,
    RunStatus,
    SchemaSource,
    StoredBrief,
    SuggestionRun,
)
from jesinia.apps.issues.application.queues import (
    BandView,
    ItemView,
    QueueApproval,
    QueueFill,
    QueueSummary,
    QueueView,
    QueueWarning,
)
from jesinia.apps.issues.application.reads import IssueDetail
from jesinia.apps.issues.application.suggest import MAX_BACKLOG_REFS
from jesinia.apps.issues.application.themes import (
    THEME_ISSUES_MAX,
    ThemeAdded,
    ThemedIssue,
    ThemePublish,
    ThemeSummary,
)
from jesinia.apps.issues.domain.changes import (
    AddArea,
    AddLabel,
    BatchStatus,
    Close,
    Comment,
    CreateIssue,
    EnsureLabel,
    Existing,
    IssueChange,
    New,
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
from jesinia.apps.issues.domain.epics import (
    Blocker,
    ConversionPlan,
    ItemKind,
    Member,
    PlannedChild,
    Readiness,
    RefusalCode,
    member_blocker,
)
from jesinia.apps.issues.domain.labels import (
    CreateLabel,
    LabelOp,
    LabelPlan,
    LabelPlanKind,
    skip_cause,
)
from jesinia.apps.issues.domain.queues import (
    NOTE_MAX,
    QUEUE_DESCRIPTION_MAX,
    QUEUE_NAME_MAX,
    DoneThen,
    QueueItem,
)
from jesinia.apps.issues.domain.schema import (
    MAX_ALIASES,
    MAX_ENTRIES,
    MAX_LABEL_NAME,
    Canonical,
    LabelSchema,
)
from jesinia.apps.issues.domain.suggestions import (
    PriorityRun,
    Suggestion,
    SuggestionKind,
    SuggestionState,
)
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
      ``current`` the priority the issue had, which approving applies against. After an
      approval each item also has ``outcome: {state, cause}``: ``applied``, ``failed`` or
      ``skipped`` (not applied, with the cause; the next approval tries only these) or
      ``unchanged`` (nothing to set). The draft is ``approved`` once every item is applied or
      unchanged; until then ``apply_error`` lists what applied and what didn't.
    - ``queue_next`` (``scope`` ``queue_next:<queue>``): ``{queue, items: [{kind, repo, n,
      reason}]}``, items to add; ``queue_order``: the same shape, the whole proposed order;
      ``queue_split`` (``scope`` ``queue_split:<queues>``): ``{queues, items: [{kind, repo, n,
      from, to, reason}]}``. Approve these with ``/queues/suggestions/{id}/approve``: they only
      change the local queues.

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
    for. When a ``scope`` was, ``ask_run`` is the latest background ask about it (a priority,
    groups, split or brief ask)."""

    suggestions: list[IssueSuggestionOut]
    run: IssueSuggestionRunOut | None
    ask_run: IssueAskRunOut | None = None


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


class IssueAskRunOut(BaseModel):
    """A background ask of the agent (ruling IR50): a priority ask for a batch of issues, a
    grouping ask, a split ask or an epic's brief. ``scope`` names it (its drafts carry it);
    ``status`` is ``running``, ``done`` (``suggestion_id`` is the draft, ``null`` when the reply
    proposed nothing), ``waiting`` (no agent runner; ask again) or ``failed`` (``cause``, "inputs
    changed, ask again" included)."""

    scope: str
    status: Literal["running", "done", "waiting", "failed"]
    cause: str | None
    updated_at: datetime
    suggestion_id: str | None

    @classmethod
    def of(cls, run: PriorityRun) -> IssueAskRunOut:
        return cls(
            scope=run.scope,
            status=run.status,
            cause=run.cause,
            updated_at=run.updated_at,
            suggestion_id=run.suggestion_id,
        )


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


class AddLabelBody(BaseModel):
    """Add a free-form label (``theme:``, ``review:``, ``campaign:``), as publishing a theme
    stages it. Like every label it must exist on GitHub by then (its setup step creates it)."""

    kind: Literal["add_label"]
    name: LabelText


class EnsureLabelBody(BaseModel):
    """A repo's label setup step, as publishing a theme stages it: the label is created when the
    tray is applied, before every issue's steps, and only then (IR4)."""

    kind: Literal["ensure_label"]
    repo: str
    name: str
    color: str
    description: str


PendingChangeBody = Annotated[
    SetDimensionBody
    | AddAreaBody
    | RemoveAreaBody
    | AddLabelBody
    | SetParentBody
    | ReorderChildBody
    | CloseBody
    | CommentBody
    | EnsureLabelBody,
    Field(discriminator="kind"),
]


def _target_body(target: Target | None) -> IssueTargetBody | None:
    """A staged change's issue: always an existing one (the tray holds no ``CreateIssue``, so
    staging refuses a new issue's temp id)."""
    if target is None:
        return None
    existing = cast(Existing, target)
    return IssueTargetBody(repo=existing.repo, n=existing.n)


def change_body(change: IssueChange | EnsureLabel) -> PendingChangeBody:
    """A staged change as the API shows it."""
    if isinstance(change, EnsureLabel):
        return EnsureLabelBody(
            kind="ensure_label",
            repo=change.repo,
            name=change.name,
            color=change.color,
            description=change.description,
        )
    if isinstance(change, AddLabel):
        return AddLabelBody(kind="add_label", name=change.name)
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
    """A staged change: to the issue ``repo#n``, or (``n`` is ``null``) the repo's label setup
    step (``ensure_label``), which runs before every issue's steps."""

    id: str
    repo: str
    n: int | None
    change: PendingChangeBody
    source: PendingSource
    created_at: datetime

    @classmethod
    def of(cls, pending: PendingChange) -> PendingOut:
        return cls(
            id=pending.id,
            repo=pending.repo,
            n=None if pending.target is None else pending.target.n,
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
    """A batch and its result. ``status``: ``running`` (poll ``GET /pending/{id}``; steps not
    reached yet are ``skipped``, ``not run yet``), ``finished`` (the result is final) or
    ``interrupted`` (a stop cut it off). ``stopped`` is set when GitHub stopped it (a rate
    limit, with ``rate_limited_until``, or a rejected token). An interrupted or stopped batch is
    resumed only with ``POST /pending/{id}/retry`` (its changes already left the tray); steps
    already done are never repeated."""

    id: str
    origin: ApplyOrigin
    status: BatchStatus
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
            status=applied.status,
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


# Epics, conversion, briefs and milestones (Task 8) --------------------------------------------


class BlockerOut(BaseModel):
    """An issue in the way of an item's readiness, and why."""

    repo: str
    n: int
    reason: str

    @classmethod
    def of(cls, blocker: Blocker) -> BlockerOut:
        return cls(repo=blocker.repo, n=blocker.n, reason=blocker.reason)


class ReadinessOut(BaseModel):
    """Whether an item is ready for an agent: ``blockers`` name each issue in the way;
    ``warning`` is "won't fit one PR" when an epic's children span repos; ``note`` says why an
    item with nothing to check isn't ready (no children, children never fetched, all closed)."""

    ready: bool
    blockers: list[BlockerOut]
    cross_repo: bool
    warning: str | None
    note: str | None

    @classmethod
    def of(cls, readiness: Readiness) -> ReadinessOut:
        return cls(
            ready=readiness.ready,
            blockers=[BlockerOut.of(b) for b in readiness.blockers],
            cross_repo=readiness.cross_repo,
            warning=readiness.warning,
            note=readiness.note,
        )


class RefusalOut(BaseModel):
    code: RefusalCode
    reason: str


class ContainmentOut(BaseModel):
    """Whether a drop onto an epic may happen ("can't drop here" shows ``refusal.reason``);
    ``warning`` is "won't fit one PR" for a child in another repo."""

    allowed: bool
    refusal: RefusalOut | None
    warning: str | None

    @classmethod
    def of(cls, found: Containment) -> ContainmentOut:
        refusal = found.refusal
        return cls(
            allowed=refusal is None,
            refusal=None
            if refusal is None
            else RefusalOut(code=refusal.code, reason=refusal.reason),
            warning=found.warning,
        )


class EpicOut(BaseModel):
    """An epic (or initiative) in the Epics list: ``done`` closed children of ``total``,
    readiness, and ``lacks_label`` when it has children but no ``epic`` label (Jesinia suggests
    adding it)."""

    repo: str
    number: int
    title: str
    url: str
    labels: list[str]
    priority: str | None
    is_initiative: bool
    lacks_label: bool
    done: int
    total: int
    readiness: ReadinessOut
    updated_at: datetime

    @classmethod
    def of(cls, summary: EpicSummary) -> EpicOut:
        issue = summary.issue
        return cls(
            repo=issue.repo,
            number=issue.number,
            title=issue.title,
            url=issue.url,
            labels=list(issue.labels),
            priority=summary.triage.priority,
            is_initiative=summary.is_initiative,
            lacks_label=summary.lacks_label,
            done=summary.done,
            total=summary.total,
            readiness=ReadinessOut.of(summary.readiness),
            updated_at=issue.updated_at,
        )


class EpicsOut(BaseModel):
    epics: list[EpicOut]


class MemberOut(BaseModel):
    """A child of an epic, or an open issue of a milestone. ``cached`` is false for one in a
    repo nobody added (its title and state are unknown then); ``blocker`` says why it isn't
    ready (``None``: ready, or closed); ``warning`` is "won't fit one PR"."""

    repo: str
    number: int
    cached: bool
    title: str | None
    url: str | None
    state: str | None
    readiness: str | None
    complete: bool | None
    blocker: str | None
    warning: str | None = None

    @classmethod
    def of(cls, cached: CachedIssue, warning: str | None = None) -> MemberOut:
        issue = cached.issue
        is_open = issue.state == "open"
        member = Member(issue.repo, issue.number, issue.title, is_open, cached.triage)
        return cls(
            repo=issue.repo,
            number=issue.number,
            cached=True,
            title=issue.title,
            url=issue.url,
            state=issue.state,
            readiness=cached.triage.readiness,
            complete=cached.triage.complete,
            blocker=member_blocker(member) if is_open else None,
            warning=warning,
        )

    @classmethod
    def child(cls, found: EpicChild) -> MemberOut:
        if found.cached is not None:
            return cls.of(found.cached, found.warning)
        return cls(
            repo=found.ref.repo,
            number=found.ref.number,
            cached=False,
            title=None,
            url=None,
            state=None,
            readiness=None,
            complete=None,
            blocker="not in the cache; sync its repo first",
            warning=found.warning,
        )


class EpicBriefOut(BaseModel):
    """The approved brief as stored on the epic (the ``<!-- jesinia:brief -->`` comment).
    ``state`` is ``in_flight`` while a post was cut off (the next approval finds it again)."""

    comment_id: int | None
    url: str | None
    text: str
    state: BriefState
    posted_at: datetime | None
    updated_at: datetime

    @classmethod
    def of(cls, brief: StoredBrief) -> EpicBriefOut:
        return cls(
            comment_id=brief.comment_id,
            url=brief.url,
            text=brief.text,
            state=brief.state,
            posted_at=brief.posted_at,
            updated_at=brief.updated_at,
        )


class EpicDetailOut(BaseModel):
    """An epic's page: the children in order (``null`` while never fetched; refresh the item),
    the approved brief, a brief composed from the cache (``default_brief``, what Copy brief
    offers before one is approved) and how many checklist items its body has (convert them with
    ``GET``/``POST …/conversion``). ``body`` is the issue author's text, shown as data."""

    epic: EpicOut
    body: str
    children: list[MemberOut] | None
    brief: EpicBriefOut | None
    default_brief: str
    checklist_items: int

    @classmethod
    def of(cls, detail: EpicDetail) -> EpicDetailOut:
        children = detail.children
        return cls(
            epic=EpicOut.of(detail.summary),
            body=detail.summary.issue.body,
            children=None if children is None else [MemberOut.child(c) for c in children],
            brief=None if detail.brief is None else EpicBriefOut.of(detail.brief),
            default_brief=detail.default_brief,
            checklist_items=detail.checklist_items,
        )


class ConversionChildOut(BaseModel):
    """A child the conversion links: an existing issue (``repo`` and ``n``) or one it creates
    (``temp_id``, ``is_new``), with its title and a warning for another repo."""

    item: str
    repo: str | None
    n: int | None
    temp_id: str | None
    title: str
    is_new: bool
    warning: str | None

    @classmethod
    def of(cls, child: PlannedChild) -> ConversionChildOut:
        target = child.target
        existing = target if isinstance(target, Existing) else None
        return cls(
            item=child.item,
            repo=None if existing is None else existing.repo,
            n=None if existing is None else existing.n,
            temp_id=target.temp_id if isinstance(target, New) else None,
            title=child.title,
            is_new=child.is_new,
            warning=child.warning,
        )


class SkippedItemOut(BaseModel):
    item: str
    reason: str


class ConversionPlanOut(BaseModel):
    """What converting the checklist would do, from the cache; nothing is written. Approve it
    with ``digest`` (``POST`` the same path): the plan must still be this one."""

    repo: str
    n: int
    digest: str
    children: list[ConversionChildOut]
    skipped: list[SkippedItemOut]

    @classmethod
    def of(cls, repo: str, n: int, plan: ConversionPlan) -> ConversionPlanOut:
        return cls(
            repo=repo,
            n=n,
            digest=plan.digest,
            children=[ConversionChildOut.of(c) for c in plan.children],
            skipped=[SkippedItemOut(item=s.item, reason=s.reason) for s in plan.skipped],
        )


class ConversionIn(BaseModel):
    """The approval of a previewed conversion: its ``digest``."""

    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EpicBriefIn(BaseModel):
    """The brief the author approves (edited or as drafted); ``suggestion_id`` is the
    ``brief`` draft it came from, marked approved once posted. ``overwrite`` replaces a comment
    edited on GitHub since it was posted (refused otherwise)."""

    text: str = Field(min_length=1, max_length=COMMENT_BODY_LIMIT)
    suggestion_id: str | None = Field(default=None, min_length=1, max_length=64)
    overwrite: bool = False


class GroupAskIn(BaseModel):
    """Which open, ungrouped issues to group: one repo's, or every watched repo's (``null``)."""

    repo: str | None = Field(default=None, min_length=3, max_length=REPO_MAX)


class MilestoneOut(BaseModel):
    """A milestone as a queue item: its open issues' count and rolled-up readiness."""

    repo: str
    number: int
    title: str
    state: str
    due_on: datetime | None
    open_issues: int
    readiness: ReadinessOut

    @classmethod
    def of(cls, item: MilestoneItem) -> MilestoneOut:
        m = item.milestone
        return cls(
            repo=item.repo,
            number=m.number,
            title=m.title,
            state=m.state,
            due_on=m.due_on,
            open_issues=item.open_issues,
            readiness=ReadinessOut.of(item.readiness),
        )


class MilestonesOut(BaseModel):
    milestones: list[MilestoneOut]


class MilestoneDetailOut(BaseModel):
    """A milestone with its open issues and the brief built from them."""

    milestone: MilestoneOut
    issues: list[MemberOut]
    brief: str

    @classmethod
    def of(cls, item: MilestoneItem) -> MilestoneDetailOut:
        return cls(
            milestone=MilestoneOut.of(item),
            issues=[MemberOut.of(c) for c in item.issues],
            brief=item.brief,
        )


# Work queues (Task 9) -------------------------------------------------------------------------

QueueId = Annotated[str, Field(min_length=1, max_length=64)]


class WorkQueueIn(BaseModel):
    """A new queue: its name (unique, without case) and an optional description."""

    name: str = Field(min_length=1, max_length=QUEUE_NAME_MAX)
    description: str = Field(default="", max_length=QUEUE_DESCRIPTION_MAX)


class WorkQueuePatchIn(BaseModel):
    """What to change about a queue; an omitted field stays. ``position`` is its place in the
    list of queues (0 first)."""

    name: str | None = Field(default=None, min_length=1, max_length=QUEUE_NAME_MAX)
    description: str | None = Field(default=None, max_length=QUEUE_DESCRIPTION_MAX)
    position: int | None = Field(default=None, ge=0, le=1000)


class WorkQueueItemIn(BaseModel):
    """An item to add: a cached issue or epic (``n`` its number; the cache decides which it is),
    or a milestone (``n`` its number), at ``position`` among the open items (the end when
    omitted)."""

    kind: ItemKind
    repo: str = Field(min_length=3, max_length=REPO_MAX)
    n: int = Field(ge=1, le=999_999_999)
    position: int | None = Field(default=None, ge=0, le=1000)
    note: str | None = Field(default=None, max_length=NOTE_MAX)


class WorkQueueItemPatchIn(BaseModel):
    """Move an open item to ``position`` among the open items and/or change its note."""

    position: int | None = Field(default=None, ge=0, le=1000)
    note: str | None = Field(default=None, max_length=NOTE_MAX)


class WorkQueueDoneIn(BaseModel):
    """Done: what to stage on GitHub. ``close`` closes the issue, with ``comment`` (the author's
    words, posted as written); ``agent_ready`` sets its readiness so an agent can take over."""

    then: DoneThen
    comment: str | None = Field(default=None, max_length=COMMENT_BODY_LIMIT)


class WorkQueueBandIn(BaseModel):
    """A drop into a priority band (a priority label of the item's repo) at ``position`` among
    that band's items (its end when omitted)."""

    band: str = Field(min_length=1, max_length=MAX_LABEL_NAME)
    position: int | None = Field(default=None, ge=0, le=1000)


class WorkQueueSplitAskIn(BaseModel):
    """The queues to split items across."""

    queue_ids: list[QueueId] = Field(min_length=2, max_length=20)


class WorkQueueFromPriorityIn(BaseModel):
    """Turn a priority draft's order into a queue: a new one called ``name`` (with
    ``description``), or the existing ``queue_id`` (appended); exactly one of the two."""

    suggestion_id: QueueId
    queue_id: QueueId | None = None
    name: str | None = Field(default=None, min_length=1, max_length=QUEUE_NAME_MAX)
    description: str = Field(default="", max_length=QUEUE_DESCRIPTION_MAX)

    @model_validator(mode="after")
    def _one_target(self) -> WorkQueueFromPriorityIn:
        if (self.queue_id is None) == (self.name is None):
            raise ValueError("give either name (a new queue) or queue_id (an existing one)")
        return self


class WorkQueueItemOut(BaseModel):
    """A queue item. ``title``, ``state`` and ``url`` come from the cache (``null`` when it
    isn't cached; titles are the issue authors' text). ``priority`` is its label and
    ``pending_priority`` one staged in the tray but not applied yet. An open item has its
    ``readiness`` and a ``warning`` saying why it isn't ready (warned, never hidden). A done one
    has ``done_at`` and ``done_pending``, the staged change Undo takes back (``null`` when its
    issue closed on GitHub)."""

    kind: ItemKind
    repo: str
    n: int
    position: int
    note: str
    added_at: datetime
    done_at: datetime | None
    done_pending: str | None
    title: str | None
    state: str | None
    url: str | None
    priority: str | None
    pending_priority: str | None
    readiness: ReadinessOut | None
    warning: str | None

    @classmethod
    def of(cls, view: ItemView) -> WorkQueueItemOut:
        item: QueueItem = view.item
        return cls(
            kind=item.ref.kind,
            repo=item.ref.repo,
            n=item.ref.n,
            position=item.position,
            note=item.note,
            added_at=item.added_at,
            done_at=item.done_at,
            done_pending=item.done_pending,
            title=view.title,
            state=view.state,
            url=view.url,
            priority=view.priority,
            pending_priority=view.pending_priority,
            readiness=None if view.readiness is None else ReadinessOut.of(view.readiness),
            warning=view.warning,
        )


class WorkQueueBandOut(BaseModel):
    """The open items of one priority (``value``; ``null`` for none), in queue order."""

    value: str | None
    items: list[WorkQueueItemOut]

    @classmethod
    def of(cls, band: BandView) -> WorkQueueBandOut:
        return cls(value=band.value, items=[WorkQueueItemOut.of(i) for i in band.items])


class WorkQueueOut(BaseModel):
    """A queue: its open ``items`` in order, the ``done`` ones (most recently done first, the
    collapsed Done group) and the open items grouped by priority (``bands``, highest first)."""

    id: str
    name: str
    description: str
    position: int
    created_at: datetime
    items: list[WorkQueueItemOut]
    done: list[WorkQueueItemOut]
    bands: list[WorkQueueBandOut]

    @classmethod
    def of(cls, view: QueueView) -> WorkQueueOut:
        q = view.queue
        return cls(
            id=q.id,
            name=q.name,
            description=q.description,
            position=q.position,
            created_at=q.created_at,
            items=[WorkQueueItemOut.of(i) for i in view.items],
            done=[WorkQueueItemOut.of(i) for i in view.done],
            bands=[WorkQueueBandOut.of(b) for b in view.bands],
        )


class WorkQueueSummaryOut(BaseModel):
    """A queue in the list (its section tab), with counts."""

    id: str
    name: str
    description: str
    position: int
    open: int
    done: int
    warnings: int

    @classmethod
    def of(cls, summary: QueueSummary) -> WorkQueueSummaryOut:
        q = summary.queue
        return cls(
            id=q.id,
            name=q.name,
            description=q.description,
            position=q.position,
            open=summary.open,
            done=summary.done,
            warnings=summary.warnings,
        )


class WorkQueuesOut(BaseModel):
    queues: list[WorkQueueSummaryOut]


class WorkQueueActionOut(BaseModel):
    """A queue after Done or a band move, and the change staged in the tray (``null`` when a
    band move stayed within the item's band)."""

    queue: WorkQueueOut
    staged: PendingOut | None


class WorkQueueWarningOut(BaseModel):
    kind: ItemKind
    repo: str
    n: int
    reason: str

    @classmethod
    def of(cls, warning: QueueWarning) -> WorkQueueWarningOut:
        ref = warning.item.ref
        return cls(kind=ref.kind, repo=ref.repo, n=ref.n, reason=warning.reason)


class WorkQueueWarningsOut(BaseModel):
    """The open items that aren't ready for an agent, each with why."""

    warnings: list[WorkQueueWarningOut]


class WorkQueueApprovalOut(BaseModel):
    """An approved queue proposal: the suggestion, the queues it changed (locally), and what it
    skipped because things changed since."""

    suggestion: IssueSuggestionOut
    queues: list[WorkQueueOut]
    skipped: list[str]

    @classmethod
    def of(cls, approval: QueueApproval) -> WorkQueueApprovalOut:
        return cls(
            suggestion=IssueSuggestionOut.of(approval.suggestion),
            queues=[WorkQueueOut.of(v) for v in approval.views],
            skipped=list(approval.skipped),
        )


class WorkQueueFillOut(BaseModel):
    """A queue made or filled from a priority draft's order, and the issues skipped."""

    queue: WorkQueueOut
    skipped: list[str]

    @classmethod
    def of(cls, fill: QueueFill) -> WorkQueueFillOut:
        return cls(queue=WorkQueueOut.of(fill.view), skipped=list(fill.skipped))


# Themes (Task 10) ----------------------------------------------------------------------------


class ThemeIssuesIn(BaseModel):
    """The cached issues to give the theme, or take it off (a multi-selection drop)."""

    issues: list[IssueTargetBody] = Field(min_length=1, max_length=THEME_ISSUES_MAX)

    def refs(self) -> list[tuple[str, int]]:
        return [(found.repo, found.n) for found in self.issues]


class ThemeAddOut(BaseModel):
    """A theme added: its catalog ``tag`` and the issues that didn't have it yet (an Undo
    removes those, locally; the catalog entry stays)."""

    tag: str
    added: list[IssueTargetBody]

    @classmethod
    def of(cls, added: ThemeAdded) -> ThemeAddOut:
        return cls(tag=added.tag, added=[IssueTargetBody(repo=r, n=n) for r, n in added.added])


class ThemeRemoveOut(BaseModel):
    """How many of the issues had the theme (taken off locally; the catalog entry stays)."""

    removed: int


class ThemeSummaryOut(BaseModel):
    """A theme; ``publishable`` is false when ``theme:<tag>`` is too long for a GitHub label (it
    stays a local theme and filter)."""

    tag: str
    count: int
    publishable: bool

    @classmethod
    def of(cls, theme: ThemeSummary) -> ThemeSummaryOut:
        return cls(tag=theme.tag, count=theme.count, publishable=theme.publishable)


class ThemesOut(BaseModel):
    """Every theme with its number of issues, by tag (the column of drop targets)."""

    themes: list[ThemeSummaryOut]


class ThemedIssueOut(BaseModel):
    """A theme's issue; ``title``, ``state`` and ``url`` are ``null`` while it isn't cached.
    ``title`` is the issue author's text, shown as data."""

    repo: str
    n: int
    title: str | None
    state: str | None
    url: str | None

    @classmethod
    def of(cls, found: ThemedIssue) -> ThemedIssueOut:
        return cls(repo=found.repo, n=found.n, title=found.title, state=found.state, url=found.url)


class ThemeOut(BaseModel):
    """A theme and its issues (the theme as a filter)."""

    tag: str
    issues: list[ThemedIssueOut]


class ThemePublishIn(BaseModel):
    """The repo to publish the theme's ``theme:<tag>`` label in."""

    repo: str = Field(min_length=3, max_length=REPO_MAX)


class ThemePublishOut(BaseModel):
    """What publishing staged in the tray (or found staged): the label setup step and the label
    additions. Nothing reaches GitHub until the tray is applied; Undo each change there."""

    tag: str
    repo: str
    label: str
    ensure: PendingOut
    added: list[PendingOut]
    already: list[IssueTargetBody]

    @classmethod
    def of(cls, published: ThemePublish) -> ThemePublishOut:
        return cls(
            tag=published.tag,
            repo=published.repo,
            label=published.label,
            ensure=PendingOut.of(published.ensure),
            added=[PendingOut.of(c) for c in published.added],
            already=[IssueTargetBody(repo=r, n=n) for r, n in published.already],
        )


class ThemeEpicCreateOut(BaseModel):
    """A plan's epic that the batch creates: its ``title`` and ``body`` (no labels)."""

    kind: Literal["create"] = "create"
    title: str
    body: str


class ThemeEpicExistingOut(BaseModel):
    """A plan's epic that exists already (the theme's latest epic, IR59): the children are
    linked under ``repo#n``, and nothing is created."""

    kind: Literal["existing"] = "existing"
    repo: str
    n: int


class ThemeEpicPlanOut(BaseModel):
    """What turning the theme into an epic would do, from the cache; nothing is written.
    ``epic`` is either one the batch creates in ``repo`` (``kind`` ``create``) or the theme's
    epic, reused (``kind`` ``existing``); the ``children`` are linked under it, and
    ``skipped`` says why others aren't. Approve it with ``digest``."""

    tag: str
    repo: str
    epic: Annotated[ThemeEpicCreateOut | ThemeEpicExistingOut, Field(discriminator="kind")]
    digest: str
    children: list[ConversionChildOut]
    skipped: list[SkippedItemOut]

    @classmethod
    def of(cls, tag: str, plan: ConversionPlan) -> ThemeEpicPlanOut:
        planned = plan.epic
        epic: ThemeEpicCreateOut | ThemeEpicExistingOut
        if isinstance(planned, CreateIssue):
            repo = planned.repo
            epic = ThemeEpicCreateOut(title=planned.title, body=planned.body)
        else:
            repo = planned.repo
            epic = ThemeEpicExistingOut(repo=planned.repo, n=planned.number)
        return cls(
            tag=tag,
            repo=repo,
            epic=epic,
            digest=plan.digest,
            children=[ConversionChildOut.of(c) for c in plan.children],
            skipped=[SkippedItemOut(item=s.item, reason=s.reason) for s in plan.skipped],
        )


class ThemeEpicIn(BaseModel):
    """The approval of a previewed theme epic: its repo and ``digest``; ``new_epic`` when the
    preview asked for a new epic beside the theme's existing one (ruling IR59)."""

    repo: str = Field(min_length=3, max_length=REPO_MAX)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_epic: bool = False


# Abandoning an unfinished batch (ruling IR58) ----------------------------------------------------


class CreatedRefBody(BaseModel):
    """An issue a batch created, by its temp id."""

    temp_id: str = Field(min_length=1, max_length=64)
    repo: str = Field(min_length=3, max_length=REPO_MAX)
    n: int = Field(ge=1, le=999_999_999)


class AbandonIn(BaseModel):
    """Abandoning a batch: ``created`` confirms issues found on GitHub for creates whose result
    is unknown (cut off while being sent)."""

    created: list[CreatedRefBody] = Field(default_factory=list, max_length=100)


class AbandonedOut(BaseModel):
    """An abandoned batch's ledger row: its ``origin`` (the target a new approval plans for) and
    every issue it created, reused by that approval instead of created again."""

    batch_id: str
    origin: str
    state: CreationState
    created: list[CreatedRefBody]

    @classmethod
    def of(cls, origin: str, row: OriginBatch) -> AbandonedOut:
        return cls(
            batch_id=row.batch_id,
            origin=origin,
            state=row.state,
            created=[
                CreatedRefBody(temp_id=temp, repo=repo, n=n)
                for temp, (repo, n) in sorted(row.created.items())
            ],
        )
