# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response bodies of ``/api/issues`` (the generated web client's types). Issue
titles, bodies, labels and comments are the issue authors' text: the PWA shows it as data."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from jesinia.apps.issues.application.labels import LabelRun, OpResult
from jesinia.apps.issues.application.ports import RepoState, SchemaSource
from jesinia.apps.issues.application.reads import IssueDetail
from jesinia.apps.issues.domain.labels import (
    CreateLabel,
    LabelOp,
    LabelPlan,
    LabelPlanKind,
    Relabel,
)
from jesinia.apps.issues.domain.schema import (
    MAX_ALIASES,
    MAX_ENTRIES,
    MAX_LABEL_NAME,
    Canonical,
    LabelSchema,
)
from jesinia.apps.issues.domain.triage import IssueRow, IssueView
from jesinia.services.github.domain.issue import IssueComment, TimelineEvent

REPO_MAX = 140
PLAN_ID_MAX = 64

LabelName = Field(min_length=1, max_length=MAX_LABEL_NAME)


class IssuesErrorOut(BaseModel):
    """A typed error: its class name, the cause, and (when GitHub rate limited the request)
    the seconds to wait, also sent as ``Retry-After``."""

    error: str
    detail: str
    retry_after: int | None = None


class LabelCanonicalBody(BaseModel):
    """A canonical label value and the existing labels that mean the same thing."""

    name: str = LabelName
    aliases: list[str] = Field(default_factory=list, max_length=MAX_ALIASES)

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
    free_prefixes: list[str] = Field(default_factory=list, max_length=MAX_ENTRIES)

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
    """Settings → Issues for one repo. PUT replaces: ``settings_schema`` left out (or null)
    clears the schema kept in Settings, so the default applies when there is no labels.yml."""

    watched: bool
    settings_schema: LabelSchemaBody | None = None


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


class LabelReplaceOut(BaseModel):
    old: str
    new: str


class LabelOpOut(BaseModel):
    """One step: ``create`` (``name``, ``color``, ``description``), ``relabel`` (issue
    ``number``, each ``replace``) or ``delete`` (``name``)."""

    op: Literal["create", "relabel", "delete"]
    name: str | None
    color: str | None
    description: str | None
    number: int | None
    replace: list[LabelReplaceOut]

    @classmethod
    def of(cls, op: LabelOp) -> LabelOpOut:
        if isinstance(op, CreateLabel):
            return cls(
                op="create",
                name=op.name,
                color=op.color,
                description=op.description,
                number=None,
                replace=[],
            )
        if isinstance(op, Relabel):
            return cls(
                op="relabel",
                name=None,
                color=None,
                description=None,
                number=op.number,
                replace=[LabelReplaceOut(old=old, new=new) for old, new in op.replace],
            )
        return cls(op="delete", name=op.name, color=None, description=None, number=None, replace=[])


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
    op: LabelOpOut
    state: Literal["done", "failed", "skipped"]
    cause: str | None

    @classmethod
    def of(cls, result: OpResult) -> LabelStepOut:
        return cls(op=LabelOpOut.of(result.op), state=result.state, cause=result.cause)


class LabelApplyOut(BaseModel):
    """What an apply did, step by step. ``note`` says why old labels were kept;
    ``rate_limited_until`` is set when GitHub stopped the batch (the response is then a 503
    with ``Retry-After``). Anything left needs a fresh preview."""

    repo: str
    kind: LabelPlanKind
    complete: bool
    steps: list[LabelStepOut]
    note: str | None
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
            rate_limited=run.is_rate_limited,
            rate_limited_until=run.rate_limited_until,
        )
