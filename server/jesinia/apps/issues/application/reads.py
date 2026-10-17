# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cache-only reads for the REST item view, Settings and the MCP tools (spec §6.3 "Always
cached"): served from ``issues.sqlite``, never awaiting GitHub. Issue and comment text is
untrusted data, returned verbatim; the MCP tools fence it."""

from __future__ import annotations

from dataclasses import dataclass

from jesinia.apps.issues.application.ports import CachedIssue, IssueCache, RepoState
from jesinia.apps.issues.domain.triage import IssueNotCachedError, IssueRow
from jesinia.services.github.domain.issue import (
    IssueComment,
    IssueRef,
    TimelineEvent,
    validate_issue_number,
)
from jesinia.services.github.domain.repo import validate_repo

SEARCH_LIMIT = 50


@dataclass(frozen=True)
class IssueDetail:
    """One cached issue with what ``refresh_details`` last fetched for it. ``children`` is
    ``None`` while an epic's sub-issues were never fetched."""

    cached: CachedIssue
    comments: list[IssueComment]
    timeline: list[TimelineEvent]
    children: list[IssueRef] | None


class IssueQueries:
    """Reads over the cache (``SqliteIssuesStore``)."""

    def __init__(self, cache: IssueCache) -> None:
        self._cache = cache

    def repos(self) -> list[RepoState]:
        """Every repo the author added, watched or not."""
        return self._cache.all_repos()

    def detail(self, repo: str, n: int) -> IssueDetail:
        """Raises:
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
