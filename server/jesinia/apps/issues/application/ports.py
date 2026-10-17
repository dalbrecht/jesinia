# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Issues app needs from the outside world; implemented in ``infrastructure`` (the
store) and ``services/github`` (``IssueClient`` satisfies ``IssueReads``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from jesinia.apps.issues.domain.labels import LabelPlan
from jesinia.apps.issues.domain.schema import LabelSchema
from jesinia.apps.issues.domain.triage import (
    IssueFilters,
    IssueRow,
    PreviousTriage,
    SyncCursor,
    Triage,
    ViewName,
)
from jesinia.services.github.domain.issue import (
    Issue,
    IssueComment,
    IssueRef,
    Label,
    Milestone,
    Page,
    TimelineEvent,
)

SchemaSource = Literal["default", "repo", "settings"]


class IssueReads(Protocol):
    """The GitHub reads sync makes. Every method may raise ``GitHubError`` (a
    ``GitHubRateLimitedError`` while rate limited)."""

    async def list_issues(
        self, repo: str, since: datetime | None, page_token: str | None
    ) -> Page[Issue]: ...

    async def labels(self, repo: str) -> list[Label]: ...

    async def milestones(self, repo: str) -> list[Milestone]: ...

    async def file(self, repo: str, path: str) -> str | None: ...

    async def sub_issues(self, repo: str, n: int) -> list[Issue]: ...

    async def comments(
        self, repo: str, n: int, since: datetime | None, page_token: str | None = None
    ) -> Page[IssueComment]: ...

    async def timeline(
        self, repo: str, n: int, page_token: str | None = None
    ) -> Page[TimelineEvent]: ...


@dataclass(frozen=True)
class RepoState:
    """A repo the author added, with its schema and where its sync stands. ``schema`` is the
    effective one (``DEFAULT_SCHEMA`` until one is stored) and ``schema_source`` where it came
    from; ``settings_schema`` is the one made in Settings, kept even while a labels.yml wins."""

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


class IssueCache(Protocol):
    """The cache reads behind ``IssueQueries``; never GitHub."""

    def all_repos(self) -> list[RepoState]:
        """Every repo the author added, watched or not, by name."""

    def repo(self, repo: str) -> RepoState | None: ...

    def cached_issue(self, repo: str, n: int) -> CachedIssue | None: ...

    def comments(self, repo: str, n: int) -> list[IssueComment]: ...

    def timeline(self, repo: str, n: int) -> list[TimelineEvent]: ...

    def sub_issue_links(self, repo: str, parent: int) -> list[IssueRef] | None:
        """The epic's children in order; ``None`` while they were never fetched."""

    def search(self, query: str, repo: str | None, limit: int) -> list[IssueRow]:
        """Issues of watched repos (open and closed) whose number is ``query`` (``12`` or
        ``#12``) or whose title, body or labels contain it, case-insensitively and literally;
        most recently updated first."""


class LabelWrites(Protocol):
    """What the label setup and migration batches ask GitHub (``IssueClient`` satisfies it).
    Every method may raise ``GitHubError`` (a ``GitHubRateLimitedError`` while limited)."""

    async def labels(self, repo: str) -> list[Label]: ...

    async def get(self, repo: str, n: int) -> Issue: ...

    async def add_labels(self, repo: str, n: int, names: Sequence[str]) -> tuple[str, ...]: ...

    async def remove_label(self, repo: str, n: int, name: str) -> tuple[str, ...]: ...

    async def create_label(self, repo: str, name: str, color: str, description: str) -> Label: ...

    async def delete_label(self, repo: str, name: str) -> None: ...


class LabelPlanStore(Protocol):
    """Previewed label plans, and the repo state they are planned from."""

    def repo(self, repo: str) -> RepoState | None: ...

    def issues(self, repo: str) -> list[Issue]: ...

    def save_plan(self, plan: LabelPlan, now: datetime) -> None:
        """Keep ``plan``; plans that expired before ``now`` are dropped."""

    def take_plan(self, plan_id: str, repo: str) -> LabelPlan | None:
        """Remove and return ``repo``'s plan ``plan_id``: each plan is applied at most once."""

    def set_migrated(self, repo: str) -> None: ...
