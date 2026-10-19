# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Issues app needs from the outside world; implemented in ``infrastructure`` (the
stores), ``services/github`` (``IssueClient`` satisfies ``IssueReads``) and ``services/agents``
(``SessionService`` satisfies ``AgentAsker``)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from jesinia.apps.issues.domain.changes import (
    BatchResult,
    BatchStatus,
    ChangeBatch,
    PendingChange,
)
from jesinia.apps.issues.domain.epics import ItemKind
from jesinia.apps.issues.domain.labels import LabelPlan
from jesinia.apps.issues.domain.queues import Queue, QueueItem
from jesinia.apps.issues.domain.schema import LabelSchema, SchemaEntry
from jesinia.apps.issues.domain.suggestions import Suggestion, SuggestionFilters, SuggestionKind
from jesinia.apps.issues.domain.triage import (
    IssueFilters,
    IssueRow,
    PreviousTriage,
    SyncCursor,
    Triage,
    ViewName,
)
from jesinia.services.agents.application.packs import ContextPack
from jesinia.services.github.domain.issue import (
    CloseReason,
    CommentRef,
    Issue,
    IssueComment,
    IssueRef,
    Label,
    LabeledItem,
    LinkedPr,
    Milestone,
    Page,
    TimelineEvent,
)
from jesinia.services.kb.application.tags import TagCheck, TagInfo

SchemaSource = Literal["default", "repo", "settings"]


class SchemaReads(Protocol):
    async def file(self, repo: str, path: str) -> str | None:
        """A file's text on the default branch; ``None`` when it doesn't exist."""


class IssueReads(Protocol):
    """The GitHub reads sync makes. Every method may raise ``GitHubError`` (a
    ``GitHubRateLimitedError`` while rate limited)."""

    async def repository(self, repo: str) -> str:
        """GitHub's own spelling of ``repo``'s ``owner/name``."""

    async def list_issues(
        self, repo: str, since: datetime | None, page_token: str | None
    ) -> Page[Issue]: ...

    async def labels(self, repo: str) -> list[Label]: ...

    async def milestones(self, repo: str) -> list[Milestone]: ...

    async def file(self, repo: str, path: str) -> str | None: ...

    async def sub_issues(self, repo: str, n: int) -> list[Issue]: ...

    async def timeline_links(self, repo: str, n: int) -> list[LinkedPr]:
        """The pull requests that reference the issue (its whole timeline)."""

    async def comments(
        self, repo: str, n: int, since: datetime | None, page_token: str | None = None
    ) -> Page[IssueComment]: ...

    async def timeline(
        self, repo: str, n: int, page_token: str | None = None
    ) -> Page[TimelineEvent]: ...


@dataclass(frozen=True)
class RepoState:
    """A repo the author added, with its schema and where its sync stands. ``schema`` is the
    effective one (``DEFAULT_SCHEMA`` until one is stored), with the approved ``overlay``
    entries merged over it, and ``schema_source`` where it came from; ``settings_schema`` is the
    one made in Settings, kept even while a labels.yml wins."""

    repo: str
    watched: bool
    added_at: datetime
    schema_source: SchemaSource
    schema: LabelSchema
    migrated: bool
    cursor: SyncCursor
    synced_at: datetime | None
    sync_error: str | None
    settings_schema: LabelSchema | None = None
    overlay: tuple[SchemaEntry, ...] = ()


@dataclass(frozen=True)
class StoredIssue:
    """An issue as a sync writes it: GitHub's data plus its triage state."""

    issue: Issue
    triage: Triage
    inbox: bool


class IssuesStore(Protocol):
    """``issues.sqlite``. Repos are validated by the caller."""

    def watch(self, repo: str, now: datetime) -> None:
        """Watch ``repo``; a repo added before keeps its ``added_at``."""

    def unwatch(self, repo: str) -> None:
        """Stop syncing ``repo``; its cache is kept."""

    def watched_repos(self) -> list[str]: ...

    def repo(self, repo: str) -> RepoState | None: ...

    def set_schema(self, repo: str, source: SchemaSource, schema: LabelSchema) -> None:
        """Store the effective schema and where it came from."""

    def set_settings_schema(self, repo: str, schema: LabelSchema | None) -> None:
        """Keep (or clear) the schema made in Settings, apart from the effective one."""

    def replace_labels(self, repo: str, labels: Sequence[Label]) -> None: ...

    def replace_milestones(self, repo: str, milestones: Sequence[Milestone]) -> None: ...

    def previous_triage(self, repo: str, numbers: Sequence[int]) -> dict[int, PreviousTriage]: ...

    def is_unchanged(self, repo: str, issue: Issue) -> bool:
        """Whether the cache already holds exactly this version of ``issue``."""

    def save_page(
        self,
        repo: str,
        issues: Sequence[StoredIssue],
        links: Mapping[int, Sequence[Issue]],
        cursor: SyncCursor,
        now: datetime,
    ) -> None:
        """Write one page of issues, the children fetched for its epics and the cursor after
        it, in one transaction: an interrupted sync resumes from the last page written. A row
        is never replaced by an older ``updated_at``."""

    def record_sync(self, repo: str, synced_at: datetime | None, error: str | None) -> None:
        """A finished sync (``synced_at``) or a failed one (``error``; the last good
        ``synced_at`` is kept)."""

    def issues(self, repo: str) -> list[Issue]: ...

    def update_triage(self, repo: str, triaged: Sequence[tuple[int, Triage]]) -> None:
        """Store recomputed triage; the Inbox flag is cleared for issues now complete and
        never set."""

    def epics_without_links(self, repo: str, limit: int) -> list[int]: ...

    def linked_prs_due(self, repo: str, limit: int) -> list[int]:
        """Up to ``limit`` open issues, those whose linked PRs were checked longest ago (never
        first), then by number."""

    def mark_linked_prs_checked(self, repo: str, numbers: Sequence[int], now: datetime) -> None:
        """``numbers``' linked PRs were just read (and stored with their page)."""

    def set_linked_prs(self, repo: str, n: int, state: str | None, now: datetime) -> None:
        """Store a refreshed summary of ``repo#n``'s linked PRs, checked ``now``."""

    def set_sub_issue_links(
        self, repo: str, parent: int, children: Sequence[Issue], now: datetime
    ) -> None: ...

    def issue(self, repo: str, n: int) -> Issue | None: ...

    def replace_details(
        self,
        repo: str,
        n: int,
        comments: Sequence[IssueComment],
        events: Sequence[TimelineEvent],
        now: datetime,
    ) -> None: ...

    def rows(self, view: ViewName, filters: IssueFilters, now: datetime) -> list[IssueRow]:
        """Open issues of watched repos in ``view``, most recently updated first."""


@dataclass(frozen=True)
class CachedIssue:
    """An issue as the cache holds it, for the item view and the MCP tools."""

    issue: Issue
    triage: Triage
    inbox: bool
    details_synced_at: datetime | None


class ViewCache(Protocol):
    """What an Inbox or Backlog view reads (``IssuesStore`` has it too)."""

    def watched_repos(self) -> list[str]: ...

    def repo(self, repo: str) -> RepoState | None: ...

    def rows(self, view: ViewName, filters: IssueFilters, now: datetime) -> list[IssueRow]: ...


class IssueCache(ViewCache, Protocol):
    """The cache reads behind ``IssueQueries``; never GitHub."""

    def all_repos(self) -> list[RepoState]:
        """Every repo the author added, watched or not, by name."""

    def labels(self, repo: str) -> list[Label]:
        """The repo's labels as last synced."""

    def cached_issue(self, repo: str, n: int) -> CachedIssue | None: ...

    def comments(self, repo: str, n: int) -> list[IssueComment]: ...

    def timeline(self, repo: str, n: int) -> list[TimelineEvent]: ...

    def sub_issue_links(self, repo: str, parent: int) -> list[IssueRef] | None:
        """The epic's children in order; ``None`` while they were never fetched."""

    def search(self, query: str, repo: str | None, limit: int) -> list[IssueRow]:
        """Issues of watched repos (open and closed) whose number is ``query`` (``12`` or
        ``#12``) or whose title, body or labels contain it literally, ignoring case for ASCII
        letters only (SQLite ``LIKE``); most recently updated first."""


class LabelWrites(Protocol):
    """What the label setup and migration batches ask GitHub (``IssueClient`` satisfies it).
    Every method may raise ``GitHubError`` (a ``GitHubRateLimitedError`` while limited)."""

    async def labels(self, repo: str) -> list[Label]: ...

    async def file(self, repo: str, path: str) -> str | None: ...

    async def label(self, repo: str, name: str) -> Label | None:
        """One label read live; ``None`` when the repo has no such label."""

    async def labeled(self, repo: str, name: str) -> list[LabeledItem]:
        """Every issue and pull request carrying ``name`` now (``InvalidLabelError`` for a
        name with a comma, which GitHub's filter can't express)."""

    async def add_labels(self, repo: str, n: int, names: Sequence[str]) -> tuple[str, ...]: ...

    async def remove_label(self, repo: str, n: int, name: str) -> tuple[str, ...]: ...

    async def create_label(self, repo: str, name: str, color: str, description: str) -> Label: ...

    async def delete_label(self, repo: str, name: str) -> None: ...


class LabelPlanStore(Protocol):
    """Previewed label plans, and the repo state they are planned from."""

    def repo(self, repo: str) -> RepoState | None: ...

    def issue(self, repo: str, n: int) -> Issue | None: ...

    def replace_labels(self, repo: str, labels: Sequence[Label]) -> None: ...

    def issues(self, repo: str) -> list[Issue]: ...

    def set_schema(self, repo: str, source: SchemaSource, schema: LabelSchema) -> None: ...

    def update_triage(self, repo: str, triaged: Sequence[tuple[int, Triage]]) -> None:
        """Store recomputed triage; the Inbox flag is cleared for issues now complete and
        never set."""

    def update_issue_labels(self, issue: Issue, labels: Sequence[str], triaged: Triage) -> None:
        """Store a cached issue's labels as a write left them, with its new triage; the Inbox flag
        is cleared when it is now complete, never set."""

    def save_plan(self, plan: LabelPlan, now: datetime) -> None:
        """Keep ``plan``; plans that expired before ``now`` are dropped."""

    def take_plan(self, plan_id: str, repo: str) -> LabelPlan | None:
        """Remove and return ``repo``'s plan ``plan_id``: each plan is applied at most once."""

    def set_migrated(self, repo: str) -> None: ...


# Suggestions (Task 5) ------------------------------------------------------------------------

RunStatus = Literal["queued", "running", "done", "skipped", "waiting", "failed"]
RunSource = Literal["inbox", "demand"]


@dataclass(frozen=True)
class SuggestionRun:
    """An issue's latest suggestion run. ``due_at`` is when a queued (or waiting) run may
    start; ``cause`` says why it failed or waits."""

    repo: str
    n: int
    status: RunStatus
    source: RunSource
    due_at: datetime
    cause: str | None
    updated_at: datetime


@dataclass(frozen=True)
class CorpusEntry:
    """A cached issue as the similarity search and the epic list see it. ``title`` and
    ``body`` are untrusted text."""

    repo: str
    n: int
    title: str
    body: str
    is_open: bool
    is_epic: bool


class SuggestionStore(Protocol):
    """Suggestions and their runs in ``issues.sqlite``."""

    def suggestions(self, filters: SuggestionFilters, limit: int | None = None) -> list[Suggestion]:
        """Matching suggestions, oldest first (at most ``limit``)."""

    def suggestion(self, suggestion_id: str) -> Suggestion | None: ...

    def replace_drafts(self, repo: str, n: int, drafts: Sequence[Suggestion]) -> None:
        """Drop the issue's untouched (``draft``) suggestions of the per-issue kinds and store
        ``drafts`` instead, in one transaction; edited, approved and dismissed ones stay, and so
        does a draft an approval in flight claims (IR40)."""

    def replace_scope_drafts(
        self, scope: str, kind: SuggestionKind, drafts: Sequence[Suggestion]
    ) -> None:
        """Drop the untouched (``draft``) suggestions of this ``kind`` about this ``scope`` (a
        batch of issues, ruling IR11) that no approval claims and store ``drafts`` instead, in
        one transaction."""

    def save(self, suggestion: Suggestion) -> None:
        """Store an edited or dismissed suggestion (its kind, payload, reason, state, time,
        origin and basis)."""

    def retire(self, suggestion_ids: Sequence[str]) -> None:
        """Delete these suggestions if they are still untouched drafts no approval claims."""

    def is_claimed(self, suggestion_id: str) -> bool:
        """Whether an approval is applying the suggestion now (IR40)."""

    def queue_run(
        self, repo: str, n: int, due_at: datetime, source: RunSource, now: datetime
    ) -> None:
        """Queue (or re-queue) the issue's run to start at ``due_at``."""

    def due_runs(self, now: datetime, limit: int) -> list[SuggestionRun]:
        """Queued or waiting runs due by ``now``, the longest due first."""

    def set_run(
        self,
        repo: str,
        n: int,
        status: RunStatus,
        cause: str | None,
        now: datetime,
        *,
        only_if: RunStatus | None = None,
    ) -> None:
        """Record a run's status; with ``only_if``, only while it still has that status (an
        edit may have queued it again meanwhile)."""

    def run(self, repo: str, n: int) -> SuggestionRun | None: ...

    def requeue_running(self, now: datetime) -> int:
        """Queue again the runs a stop interrupted (at start nothing runs yet)."""

    def corpus(self) -> list[CorpusEntry]:
        """Every cached issue of a watched repo, open or closed; bodies cut at the length the
        similarity search indexes."""


class SuggestionCache(Protocol):
    """The issue cache a suggestion run reads (``SqliteIssuesStore`` has it)."""

    def repo(self, repo: str) -> RepoState | None: ...

    def cached_issue(self, repo: str, n: int) -> CachedIssue | None: ...


@runtime_checkable
class AgentAsker(Protocol):
    """A one-shot ask of the user's agent with no tools (``SessionService.one_shot``). The
    pack's untrusted docs reach the agent fenced as data. May raise ``RunnerUnavailable``,
    ``TurnFailed``, ``TimeoutError`` (or other agent errors)."""

    async def one_shot(
        self, runner: str, pack: ContextPack, prompt: str, *, timeout: float = 45.0
    ) -> str: ...


# The apply engine (Task 6) -------------------------------------------------------------------

# Where a batch came from: the tray, a suggestion's approval, or an epic's checklist conversion.
ApplyOrigin = Literal["pending", "suggestion", "epic"]


class IssueWrites(LabelWrites, Protocol):
    """What the apply engine asks GitHub (``IssueClient`` satisfies it). Every method may raise
    ``GitHubError`` (a ``GitHubRateLimitedError`` while limited)."""

    async def get(self, repo: str, n: int) -> Issue: ...

    async def parent(self, repo: str, n: int) -> Issue | None: ...

    async def add_sub_issue(
        self, repo: str, n: int, child_id: int, *, replace_parent: bool = False
    ) -> None: ...

    async def remove_sub_issue(self, repo: str, n: int, child_id: int) -> None: ...

    async def reprioritize_sub_issue(
        self, repo: str, n: int, child_id: int, after_id: int | None
    ) -> None: ...

    async def close(
        self, repo: str, n: int, state_reason: CloseReason, duplicate_of: int | None = None
    ) -> Issue: ...

    async def comment(self, repo: str, n: int, body: str) -> CommentRef: ...

    async def create_issue(
        self, repo: str, title: str, body: str, labels: Sequence[str]
    ) -> Issue: ...


class ApplyCache(Protocol):
    """The issue cache the apply engine reads and updates (``SqliteIssuesStore`` has it)."""

    def repo(self, repo: str) -> RepoState | None: ...

    def cached_issue(self, repo: str, n: int) -> CachedIssue | None: ...

    def add_overlay(self, repo: str, entry: SchemaEntry, now: datetime) -> None:
        """Record an approved schema entry; merged over the repo's schema on every read."""

    def replace_labels(self, repo: str, labels: Sequence[Label]) -> None: ...

    def refresh_issue(self, issue: Issue, triaged: Triage) -> None:
        """Store a cached issue as an apply left it (a row already newer is kept); the Inbox
        flag is cleared when it is now complete, never set."""


CreationState = Literal["running", "complete", "incomplete", "abandoned"]


@dataclass(frozen=True)
class OriginBatch:
    """A batch recorded for an origin that may create issues (IR56, IR57): its ``state``, the
    issues it created by temp id (as of its last save, or as the author confirmed when
    abandoning it, IR58), and the origin's ``notes`` (a conversion's item hashes to temp ids)."""

    batch_id: str
    state: CreationState
    created: Mapping[str, tuple[str, int]]
    notes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AppliedBatch:
    """A batch as applied, kept so Retry can resume it: where it came from (the tray or a
    suggestion's approval), its changes, its result so far and its ``status``; ``request_key``
    is the Idempotency-Key that started it."""

    id: str
    origin: ApplyOrigin
    suggestion_id: str | None
    batch: ChangeBatch
    result: BatchResult
    created_at: datetime
    updated_at: datetime
    status: BatchStatus = "finished"
    request_key: str | None = None


def creation_state(applied: AppliedBatch) -> CreationState:
    """``running``, ``complete`` (every step done), ``incomplete`` (finished or cut off with a
    step not done) or ``abandoned``: what an origin's ledger records of the batch (IR56)."""
    if applied.status in ("running", "abandoned"):
        return "running" if applied.status == "running" else "abandoned"
    return "complete" if applied.result.complete else "incomplete"


class ApplyStore(Protocol):
    """The pending-changes tray and the applied batches in ``issues.sqlite``."""

    def add_pending(self, change: PendingChange) -> None: ...

    def pending(self, ids: Sequence[str] | None = None) -> list[PendingChange]:
        """The staged changes (only ``ids`` when given), in the order staged."""

    def delete_pending(self, ids: Sequence[str]) -> int:
        """Remove these staged changes; how many there were."""

    def save_batch(self, applied: AppliedBatch) -> None:
        """Store (or replace) an applied batch and its result."""

    def start_batch(self, applied: AppliedBatch, taken: Sequence[str]) -> None:
        """Store a new batch and remove the staged changes it took from the tray, in one
        transaction: a crash never leaves both, or neither."""

    def batch(self, batch_id: str) -> AppliedBatch | None: ...

    def batch_by_key(self, request_key: str) -> AppliedBatch | None:
        """The batch a request with this Idempotency-Key started."""

    def recent_batches(self, limit: int) -> list[AppliedBatch]:
        """The ``limit`` most recently updated batches, newest first (abandoned and dismissed
        ones left out)."""

    def dismiss_batch(self, batch_id: str, now: datetime) -> bool:
        """Mark the batch dismissed from the tray (kept if already); False: no such batch."""

    def interrupt_running(self) -> int:
        """Mark every ``running`` batch ``interrupted`` (at start nothing runs yet)."""

    def begin_batch(self, applied: AppliedBatch, origin_key: str, notes: Mapping[str, str]) -> None:
        """Store a new batch and its ledger row for ``origin_key`` (with ``notes``) in one
        transaction (N3); every later ``save_batch`` keeps the row's state and created issues
        current."""

    def origin_batches(self, origin_key: str) -> list[OriginBatch]:
        """The batches recorded for ``origin_key``, oldest first (kept after pruning)."""

    def origin_of(self, batch_id: str) -> tuple[str, OriginBatch] | None:
        """A recorded batch's origin and ledger row (found after the batch is pruned too)."""

    def origins(self, prefix: str) -> list[tuple[str, OriginBatch]]:
        """Every recorded batch whose origin starts with ``prefix`` (``group:``, say)."""

    def forget_epic(self, batch_id: str) -> None:
        """Drop the epic a recorded batch made from its ledger row: it is gone on GitHub
        (deleted or transferred), so it is reused and blocks no more (S4)."""

    def abandon(self, batch_id: str, created: Mapping[str, tuple[str, int]], now: datetime) -> None:
        """Mark a batch abandoned in the ledger (with ``created``) and in ``apply_batches``
        when it is still stored, in one transaction (IR58)."""

    def prune_batches(self, before: datetime) -> int:
        """Forget batches last updated before ``before``; how many."""


# Epics and briefs (Task 8) -------------------------------------------------------------------

BriefState = Literal["in_flight", "posted"]


@dataclass(frozen=True)
class StoredBrief:
    """An epic's delivery brief as stored on GitHub: ``comment_id`` and ``url`` of the comment
    (``None`` until one is known), the ``body`` posted (marker included), and ``state``:
    ``in_flight`` from right before a new comment is posted until its id is stored, ``posted``
    after. ``attempt_at`` is when that post started and ``nonce`` the one its marker carries
    (a cut-off post is looked for by both, IR48); ``pending_body`` is the body an edit was on
    its way with, until it is recorded as posted."""

    repo: str
    n: int
    comment_id: int | None
    url: str | None
    body: str
    state: BriefState
    attempt_at: datetime | None
    posted_at: datetime | None
    updated_at: datetime
    nonce: str | None = None
    pending_body: str | None = None

    @property
    def text(self) -> str:
        """The brief without its marker, as the author wrote it."""
        return self.body.split("\n", 1)[1] if "\n" in self.body else ""


class BriefStore(Protocol):
    """``epic_briefs`` in ``issues.sqlite``."""

    def brief(self, repo: str, n: int) -> StoredBrief | None: ...

    def mark_in_flight(self, repo: str, n: int, body: str, now: datetime, nonce: str) -> None:
        """Record that a new brief comment carrying ``nonce`` is about to be posted (the
        checkpoint before an irreversible write); a stored comment id is forgotten (it is gone
        on GitHub)."""

    def mark_editing(self, repo: str, n: int, body: str | None, now: datetime) -> None:
        """Record the body an edit of the stored comment is about to send (the checkpoint
        before it): an edit GitHub takes whose answer is lost is known as ours next time.
        ``None`` clears it (GitHub refused the edit for certain)."""

    def mark_posted(
        self, repo: str, n: int, comment_id: int, url: str, body: str, now: datetime
    ) -> StoredBrief:
        """Record the brief comment as posted (or edited) with its id; a pending edit is settled."""


class BriefWrites(Protocol):
    """What publishing a brief asks GitHub (``IssueClient`` satisfies it). Every method may
    raise ``GitHubError`` (a ``GitHubRateLimitedError`` while limited)."""

    async def comment(self, repo: str, n: int, body: str) -> CommentRef: ...

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> CommentRef: ...

    async def get_comment(self, repo: str, comment_id: int) -> IssueComment | None:
        """One comment read live; ``None`` when it is gone."""

    async def comments(
        self, repo: str, n: int, since: datetime | None, page_token: str | None = None
    ) -> Page[IssueComment]: ...


class EpicCache(SuggestionCache, Protocol):
    """The cache the epic views, plans and asks read (``SqliteIssuesStore`` has it)."""

    def watched_repos(self) -> list[str]: ...

    def sub_issue_links(self, repo: str, parent: int) -> list[IssueRef] | None: ...

    def open_issues(self, repo: str | None) -> list[CachedIssue]:
        """Open issues of watched repos (one repo when given), most recently updated first."""

    def milestones(self, repo: str) -> list[Milestone]: ...

    def milestone_issues(self, repo: str, title: str) -> list[CachedIssue]:
        """The issues (open and closed) of the milestone titled ``title``, by number."""


class BatchReads(Protocol):
    """Stored batches and their origins' ledger (``SqliteApplyStore`` has it)."""

    def batch(self, batch_id: str) -> AppliedBatch | None: ...

    def origin_batches(self, origin_key: str) -> list[OriginBatch]: ...


class QueueStore(Protocol):
    """``queues`` and ``queue_items`` in ``issues.sqlite`` (Task 9): local only."""

    def queues(self) -> list[Queue]:
        """Every queue, in the author's order."""

    def queue(self, queue_id: str) -> Queue | None: ...

    def queue_named(self, name: str) -> Queue | None:
        """The queue called ``name`` (compared without case)."""

    def save_queue(self, queue: Queue) -> None:
        """Insert or update a queue (its name, description and position)."""

    def set_queue_order(self, queue_ids: Sequence[str]) -> None:
        """Number the queues in this order, in one transaction."""

    def delete_queue(self, queue_id: str) -> None:
        """Delete a queue and its items, in one transaction."""

    def items(self, queue_id: str) -> list[QueueItem]:
        """The queue's items, open and done, by position."""

    def save_items(self, queue_id: str, items: Sequence[QueueItem]) -> None:
        """Replace the queue's items with ``items``, in one transaction."""

    def save_many(self, queues: Mapping[str, Sequence[QueueItem]]) -> None:
        """Replace several queues' items, all in one transaction."""

    def items_with_pending(self, pending_id: str) -> list[tuple[str, QueueItem]]:
        """Every done item (in any queue) whose Done staged ``pending_id``."""

    def done_pending_ids(self) -> list[str]:
        """Every pending change a done item still names."""

    def clear_done_pending(self, pending_ids: Sequence[str]) -> None:
        """Done items stop naming ``pending_ids`` (changes that left the tray)."""

    def open_items_for(
        self, kinds: Sequence[ItemKind], refs: Sequence[str]
    ) -> list[tuple[str, QueueItem]]:
        """Every queue's open items of these kinds and refs (``owner/repo#n``), with their
        queue's id."""

    def set_done(self, queue_id: str, item: QueueItem, done_at: datetime) -> None:
        """Mark one open item done (closed on GitHub: no pending change)."""


class ThemeStore(Protocol):
    """``issue_themes`` and ``theme_epics`` in ``issues.sqlite`` (Task 10): local only."""

    def add(
        self, refs: Sequence[tuple[str, int]], tag: str, now: datetime
    ) -> list[tuple[str, int]]:
        """Give each issue the theme, in one transaction; the refs that didn't have it yet."""

    def remove(self, refs: Sequence[tuple[str, int]], tag: str) -> int:
        """Take the theme off these issues; how many had it."""

    def themes(self) -> list[tuple[str, int]]:
        """Every theme with its number of issues, by tag."""

    def issues(self, tag: str) -> list[tuple[str, int]]:
        """The theme's issues, by repo and number."""

    def of_issue(self, repo: str, n: int) -> list[str]:
        """The issue's themes, by tag."""

    def retag(self, old: str, new: str) -> None:
        """Rename a theme on every issue (a catalog rename or merge), merging into ``new`` where
        an issue has both, in one transaction; its epic batches (and their ledger origin)
        follow."""

    def history_tags(self) -> list[str]:
        """Tags kept by a theme's epic history (``theme_epics`` and its ledger origins), with or
        without issues carrying them now."""

    def record_epic(
        self, tag: str, batch_id: str, now: datetime, epic: tuple[str, int] | None = None
    ) -> None:
        """Keep a batch that turns the theme into an epic (once; recording it again is a
        no-op), with the epic it links under when that is known already."""


class ThemeTags(Protocol):
    """The KB-wide tag catalog themes use (``services.kb``'s ``TagService`` has it)."""

    async def check(self, raw: str, *, advise: bool = True) -> TagCheck:
        """Normalise ``raw`` and find it, or near matches, in the catalog; ``advise=False``
        asks no agent."""

    async def ensure(self, tags: Iterable[str]) -> list[str]:
        """The canonical catalog names for ``tags``, adding new ones all-or-nothing."""

    async def catalog(self) -> list[TagInfo]:
        """The approved and retired tags, with their aliases."""
