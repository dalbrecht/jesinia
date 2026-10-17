# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cache-only reads for the REST item view, Settings and the MCP tools (spec §6.3 "Always
cached"): served from ``issues.sqlite``, never awaiting GitHub. Issue and comment text is
untrusted data, returned verbatim; the MCP tools fence it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from jesinia.apps.issues.application.ports import CachedIssue, IssueCache, RepoState, ViewCache
from jesinia.apps.issues.domain.triage import (
    IssueFilters,
    IssueNotCachedError,
    IssueRow,
    IssueView,
    ViewName,
)
from jesinia.services.github.domain.issue import (
    IssueComment,
    IssueRef,
    TimelineEvent,
    validate_issue_number,
)
from jesinia.services.github.domain.repo import validate_repo

SEARCH_LIMIT = 50

Clock = Callable[[], datetime]
IsSyncing = Callable[[str], bool]


def build_view(
    cache: ViewCache, view: ViewName, filters: IssueFilters, now: datetime, is_syncing: IsSyncing
) -> IssueView:
    """The cached ``view`` with the listed repos' sync state reported, never awaited:
    ``synced_at`` is the oldest last good sync (``None`` while any listed repo never synced),
    ``sync_error`` joins each listed repo's last failure."""
    repos = [filters.repo] if filters.repo is not None else cache.watched_repos()
    states = [state for repo in repos if (state := cache.repo(repo)) is not None]
    synced = [state.synced_at for state in states]
    errors = [f"{state.repo}: {state.sync_error}" for state in states if state.sync_error]
    return IssueView(
        rows=cache.rows(view, filters, now),
        synced_at=None if not synced or None in synced else min(t for t in synced if t),
        syncing=any(is_syncing(repo) for repo in repos),
        sync_error="; ".join(errors) or None,
    )


@dataclass(frozen=True)
class IssueDetail:
    """One cached issue with what ``refresh_details`` last fetched for it. ``children`` is
    ``None`` while an epic's sub-issues were never fetched."""

    cached: CachedIssue
    comments: list[IssueComment]
    timeline: list[TimelineEvent]
    children: list[IssueRef] | None


class IssueQueries:
    """Reads over the cache (``SqliteIssuesStore``); they work without the github service.

    Args:
        cache: ``issues.sqlite``.
        clock: the current UTC time.
        is_syncing: whether a repo's sync is running (``SyncService.is_syncing``; never,
            without the github service).
    """

    def __init__(
        self, cache: IssueCache, clock: Clock, is_syncing: IsSyncing | None = None
    ) -> None:
        self._cache = cache
        self._clock = clock
        self._is_syncing: IsSyncing = is_syncing or (lambda repo: False)

    def view(self, view: ViewName, filters: IssueFilters) -> IssueView:
        """The cached Inbox or Backlog (see ``build_view``)."""
        return build_view(self._cache, view, filters, self._clock(), self._is_syncing)

    def repos(self) -> list[RepoState]:
        """Every repo the author added, watched or not."""
        return self._cache.all_repos()

    def detail(self, repo: str, n: int) -> IssueDetail:
        """Any cached issue, its repo watched or not: an opened issue's tab keeps working after
        its repo is unwatched (the cache is kept), while lists and search show watched repos.

        Raises:
        InvalidRepoError: ``repo`` is not ``owner/name``.
        InvalidIssueReferenceError: ``n`` isn't an issue number.
        IssueNotCachedError: ``repo#n`` hasn't been synced.
        """
        name, number = validate_repo(repo), validate_issue_number(n)
        cached = self._cache.cached_issue(name, number)
        if cached is None:
            raise IssueNotCachedError(f"{repo}#{n} is not in the cache; sync the repo first")
        return IssueDetail(
            cached,
            self._cache.comments(name, number),
            self._cache.timeline(name, number),
            self._cache.sub_issue_links(name, number),
        )

    def search(self, query: str, repo: str | None = None) -> list[IssueRow]:
        """Cached issues of watched repos matching ``query`` (see ``IssueCache.search``), at
        most ``SEARCH_LIMIT``.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
        """
        name = None if repo is None else validate_repo(repo)
        return self._cache.search(query, name, SEARCH_LIMIT)
