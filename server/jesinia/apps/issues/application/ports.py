# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Issues app needs from the outside world; implemented in ``infrastructure`` (the
store) and ``services/github`` (``IssueClient`` satisfies ``IssueReads``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

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
    """A repo the author added, with its schema and where its sync stands. ``schema`` is
    ``DEFAULT_SCHEMA`` until one is stored."""

    repo: str
    watched: bool
    added_at: datetime
    schema_source: SchemaSource
    schema: LabelSchema
    migrated: bool
    cursor: SyncCursor
    synced_at: datetime | None
    sync_error: str | None


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

    def set_schema(self, repo: str, source: SchemaSource, schema: LabelSchema) -> None: ...

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
