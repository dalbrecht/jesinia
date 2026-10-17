# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issues sync, Inbox and Backlog (spec §3, §6.2, §6.3).

``list`` always serves from ``issues.sqlite`` and never awaits GitHub or a running sync: the
app is never empty while connecting, and a sync's state is reported, not awaited. ``sync``
(the ``issues.sync`` job, every ``SYNC_INTERVAL``, and on demand) is incremental by ``since``
and resumable: every page is written with the cursor after it, so an interrupted sync picks up
where it stopped, with no issue skipped or taken twice (see ``SyncCursor``). It also refreshes
the repo's labels, milestones and label schema, and recomputes stored completeness whenever the
schema changes (ruling IR18).

What a sync asks GitHub (ruling IR20):

- **First sync of a repo** (it has never finished one): the issue list only, plus labels,
  milestones and ``.jesinia/labels.yml``. No per-issue call at all, so a 235-issue backlog costs
  three list pages; ``sub_issue_count`` comes from the list payload and ``parent`` from its
  ``parent_issue_url``.
- **Later syncs:** an epic (it has sub-issues) that changed since the last sync has its children
  fetched. Up to ``EPIC_BACKFILL_PER_SYNC`` unchanged epics whose children were never fetched are
  backfilled per sync, so links arrive without a first-sync spike.
- **Comments and timeline** are never fetched by sync: ``refresh_details`` fetches them (and an
  epic's children) on demand, when an issue is opened.

Only repos the author added are synced; nothing is watched automatically. Issue and comment
text is untrusted: it is stored verbatim as data, never interpreted.

A rate limit stops the whole run and is recorded as the repo's ``sync_error`` with GitHub's
reset time; nothing is asked again until then (or for ``SYNC_INTERVAL`` when GitHub gave no
time). Any other GitHub failure is recorded the same way. ``sync`` never raises for either: the
cache keeps serving.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from jesinia.apps.issues.application.ports import (
    IssueReads,
    IssuesStore,
    RepoState,
    SchemaSource,
    StoredIssue,
)
from jesinia.apps.issues.application.reads import build_view
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA, LabelSchema, parse_labels_yml
from jesinia.apps.issues.domain.triage import (
    IssueFilters,
    IssueNotCachedError,
    IssueView,
    RepoNotWatchedError,
    ViewName,
    is_inbox,
    triage,
)
from jesinia.services.github.application.payload import GitHubPayloadError
from jesinia.services.github.application.service import GitHubError, GitHubRateLimitedError
from jesinia.services.github.domain.issue import Issue, Page
from jesinia.services.github.domain.repo import validate_repo

log = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(minutes=5)
LABELS_YML = ".jesinia/labels.yml"
EPIC_BACKFILL_PER_SYNC = 10
# Pages of comments (and of timeline events) ``refresh_details`` reads for one issue.
DETAIL_PAGE_CAP = 10

Clock = Callable[[], datetime]
# Told which issues just entered the Inbox view (Task 5 queues suggestion jobs from it).
OnInbox = Callable[[str, Sequence[int]], Awaitable[None]]


def _without_file(settings: LabelSchema | None) -> tuple[SchemaSource, LabelSchema]:
    """The effective schema when the repo has no labels.yml (spec §2 precedence: labels.yml,
    then the schema made in Settings, then the default)."""
    return ("default", DEFAULT_SCHEMA) if settings is None else ("settings", settings)


async def _capped_pages[T](fetch: Callable[[str | None], Awaitable[Page[T]]]) -> list[T]:
    items: list[T] = []
    token: str | None = None
    for _ in range(DETAIL_PAGE_CAP):
        page = await fetch(token)
        items.extend(page.items)
        token = page.next_token
        if token is None:
            break
    return items


class SyncService:
    """Watched repos, their sync and the Inbox and Backlog views.

    Args:
        github: issue reads (``IssueClient``).
        store: ``issues.sqlite``.
        clock: the current UTC time.
        on_inbox: awaited with the issues that just entered the Inbox view; best effort, its
            failure is logged and never breaks a sync.
    """

    def __init__(
        self,
        github: IssueReads,
        store: IssuesStore,
        clock: Clock,
        *,
        on_inbox: OnInbox | None = None,
    ) -> None:
        self._github = github
        self._store = store
        self._clock = clock
        self._on_inbox = on_inbox
        self._locks: dict[str, asyncio.Lock] = {}
        self._syncing: set[str] = set()
        self._backoff_until: datetime | None = None

    # Repos ----------------------------------------------------------------------------

    async def watch(self, repo: str) -> None:
        """Add ``repo``: its existing issues will land in the Backlog, later ones in the
        Inbox. Watching it again keeps its original ``added_at``.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
        """
        self._store.watch(validate_repo(repo), self._clock())

    async def unwatch(self, repo: str) -> None:
        """Stop syncing ``repo``; its cache stays."""
        self._store.unwatch(validate_repo(repo))

    async def watched_repos(self) -> builtins.list[str]:
        return self._store.watched_repos()

    async def configure(
        self, repo: str, *, watched: bool, settings_schema: LabelSchema | None
    ) -> RepoState:
        """Settings → Issues for one repo: watch or unwatch it and keep (or clear, with
        ``None``) its Settings schema (see ``set_settings_schema``).

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: unwatching a repo the author never added.
        """
        name = validate_repo(repo)
        if watched:
            self._store.watch(name, self._clock())
        else:
            self.state(name)  # only a repo the author added can be unwatched
            self._store.unwatch(name)
        await self.set_settings_schema(name, settings_schema)
        return self.state(name)

    def state(self, repo: str) -> RepoState:
        """The stored repo, watched or not.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author never added ``repo``.
        """
        state = self._store.repo(validate_repo(repo))
        if state is None:
            raise RepoNotWatchedError(f"{repo} is not a watched repo; add it first")
        return state

    def _watched(self, repo: str) -> RepoState:
        state = self._store.repo(validate_repo(repo))
        if state is None or not state.watched:
            raise RepoNotWatchedError(f"{repo} is not a watched repo; add it first")
        return state

    # Views ----------------------------------------------------------------------------

    async def list(self, view: ViewName, filters: IssueFilters) -> IssueView:
        """The cached ``view``; never awaits GitHub or a running sync."""
        return build_view(self._store, view, filters, self._clock(), self.is_syncing)

    def is_syncing(self, repo: str) -> bool:
        return repo in self._syncing

    # Sync -----------------------------------------------------------------------------

    async def sync_all(self) -> None:
        """Sync every watched repo in turn (the ``issues.sync`` job); a rate limit stops the
        run, since every repo shares the token's limit."""
        for repo in self._store.watched_repos():
            try:
                if not await self._sync(repo):
                    return
            except RepoNotWatchedError:
                continue  # unwatched while an earlier repo synced

    async def sync(self, repo: str) -> None:
        """Sync one watched repo now. Failures are recorded, never raised; a sync of this
        repo already running makes this a no-op.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author hasn't added ``repo``.
        """
        self._watched(repo)
        await self._sync(repo)

    async def _sync(self, repo: str) -> bool:
        """Run one repo's sync; ``False`` when rate limited (the run should stop)."""
        now = self._clock()
        if self._backoff_until is not None and now < self._backoff_until:
            return False
        lock = self._locks.setdefault(repo, asyncio.Lock())
        if lock.locked():
            return True
        async with lock:
            self._syncing.add(repo)
            try:
                await self._run(self._watched(repo))
            except GitHubRateLimitedError as exc:
                self._backoff_until = exc.reset_at or now + SYNC_INTERVAL
                self._record_failure(repo, f"GitHub rate limit: {exc}")
                return False
            except GitHubError as exc:
                self._record_failure(repo, str(exc))
                return True
            finally:
                self._syncing.discard(repo)
            self._backoff_until = None
            self._store.record_sync(repo, self._clock(), None)
            return True

    def _record_failure(self, repo: str, cause: str) -> None:
        log.warning("issues.sync of %s failed, serving the cache: %s", repo, cause)
        self._store.record_sync(repo, None, cause)

    async def _run(self, state: RepoState) -> None:
        repo = state.repo
        schema = await self._refresh_schema(state)
        self._store.replace_labels(repo, await self._github.labels(repo))
        self._store.replace_milestones(repo, await self._github.milestones(repo))
        is_first = state.synced_at is None
        cursor = state.cursor
        done = False
        while not done:
            page = await self._github.list_issues(repo, cursor.since, cursor.page_token)
            # A covered issue comes back at the inclusive ``since``: re-take it only if it
            # changed (an edit within the high-water second), else skip the write and fetches.
            fresh = [
                i
                for i in page.items
                if not (cursor.covers(i.updated_at, i.id) and self._store.is_unchanged(repo, i))
            ]
            links = {} if is_first else await self._children(repo, fresh)
            stored, entered = self._triaged(state, schema, fresh)
            cursor, done = cursor.after_page(
                [(i.updated_at, i.id) for i in page.items], page.next_token
            )
            self._store.save_page(repo, stored, links, cursor, self._clock())
            if entered:
                await self._notify(repo, entered)
        if not is_first:
            for n in self._store.epics_without_links(repo, EPIC_BACKFILL_PER_SYNC):
                children = await self._github.sub_issues(repo, n)
                self._store.set_sub_issue_links(repo, n, children, self._clock())

    async def _children(
        self, repo: str, issues: Sequence[Issue]
    ) -> dict[int, builtins.list[Issue]]:
        return {
            i.number: await self._github.sub_issues(repo, i.number)
            for i in issues
            if i.sub_issue_count > 0
        }

    def _triaged(
        self, state: RepoState, schema: LabelSchema, issues: Sequence[Issue]
    ) -> tuple[builtins.list[StoredIssue], builtins.list[int]]:
        """Each issue's triage and Inbox flag, and the issues that just entered the Inbox
        view."""
        previous = self._store.previous_triage(state.repo, [i.number for i in issues])
        stored: builtins.list[StoredIssue] = []
        entered: builtins.list[int] = []
        for issue in issues:
            result = triage(issue.labels, issue.sub_issue_count, schema)
            before = previous.get(issue.number)
            inbox = is_inbox(
                before,
                created_at=issue.created_at,
                added_at=state.added_at,
                complete=result.complete,
            )
            stored.append(StoredIssue(issue, result, inbox))
            # A reopened Inbox issue counts as new, so its suggestions are asked for again.
            was_shown = (
                before is not None and before.inbox and not before.complete and before.is_open
            )
            if inbox and not result.complete and not was_shown and issue.state == "open":
                entered.append(issue.number)
        return stored, entered

    async def _notify(self, repo: str, numbers: Sequence[int]) -> None:
        if self._on_inbox is None:
            return
        try:
            await self._on_inbox(repo, numbers)
        except Exception:  # best effort: a failing listener never breaks a sync
            log.exception("issues: the Inbox listener failed for %s %s", repo, list(numbers))

    async def _refresh_schema(self, state: RepoState) -> LabelSchema:
        """The repo's schema from ``.jesinia/labels.yml``, else the one kept in settings, else
        the default; when it differs from the stored one it is stored and every cached issue
        is re-triaged."""
        source: SchemaSource
        try:
            text = await self._github.file(state.repo, LABELS_YML)
        except GitHubPayloadError as exc:
            # The file exists but can't be read as text: the default, with the cause shown.
            source, schema = "repo", replace(DEFAULT_SCHEMA, errors=(str(exc),))
        else:
            if text is None:
                # No labels.yml: the schema kept in Settings, else the default.
                source, schema = _without_file(state.settings_schema)
            else:
                source, schema = "repo", parse_labels_yml(text)
        self._store_effective(state, source, schema)
        return schema

    def _store_effective(self, state: RepoState, source: SchemaSource, schema: LabelSchema) -> None:
        """Store the effective schema and re-triage the cache when it changed; the caller
        holds the repo's lock."""
        if source != state.schema_source or schema != state.schema:
            self._store.set_schema(state.repo, source, schema)
            self._retriage(state.repo, schema)

    async def set_settings_schema(self, repo: str, schema: LabelSchema | None) -> None:
        """Keep ``schema`` as the one made in Settings for ``repo`` (``None`` clears it). It is
        stored apart from ``.jesinia/labels.yml``'s: while the file exists it wins (spec §2) and
        this only takes effect once the file is gone. When it is the effective schema the cache
        is re-triaged at once (IR18). Storing and recomputing happen under the repo's one lock,
        so a running sync can't save pages triaged with the old schema in between.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author never added ``repo``.
        """
        name = validate_repo(repo)
        async with self._locks.setdefault(name, asyncio.Lock()):
            state = self.state(name)
            self._store.set_settings_schema(name, schema)
            if state.schema_source != "repo":
                source, effective = _without_file(schema)
                self._store_effective(state, source, effective)

    def back_off(self, until: datetime | None) -> None:
        """GitHub rate limited a write (the label batches): pause syncs until ``until``, or
        for ``SYNC_INTERVAL`` without a reset time. The token's limit is shared, so a sync
        would only fail too. An earlier reset never shortens a pause already set."""
        resume = until or self._clock() + SYNC_INTERVAL
        if self._backoff_until is None or resume > self._backoff_until:
            self._backoff_until = resume

    async def recompute(self, repo: str) -> None:
        """Re-triage every cached issue of ``repo`` against its stored schema (IR18): call it
        after the schema or its overlay changes. It can clear the Inbox flag (an issue now
        complete) but never set it: the convention changed, not the issue, so this never floods
        the Inbox (IR30). It waits for a running sync of the repo, so pages that sync saves with
        the old schema can't overwrite it.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author hasn't added ``repo``.
        """
        state = self._watched(repo)
        async with self._locks.setdefault(state.repo, asyncio.Lock()):
            current = self._watched(repo)  # the schema stored by the time the lock is ours
            self._retriage(current.repo, current.schema)

    def _retriage(self, repo: str, schema: LabelSchema) -> None:
        self._store.update_triage(
            repo,
            [
                (i.number, triage(i.labels, i.sub_issue_count, schema))
                for i in self._store.issues(repo)
            ],
        )

    # Details --------------------------------------------------------------------------

    async def refresh_details(self, repo: str, n: int) -> None:
        """Fetch a cached issue's comments and timeline (at most ``DETAIL_PAGE_CAP`` pages
        each) and, for an epic, its children; replaces what was cached. Called when an issue
        is opened. GitHub failures are raised to the caller (the cache is left as it was).

        Raises:
            RepoNotWatchedError: the author hasn't added ``repo``.
            IssueNotCachedError: ``repo#n`` hasn't been synced.
            GitHubError: GitHub couldn't be read.
        """
        state = self._watched(repo)
        issue = self._store.issue(state.repo, n)
        if issue is None:
            raise IssueNotCachedError(f"{repo}#{n} is not in the cache; sync the repo first")
        comments = await _capped_pages(lambda t: self._github.comments(repo, n, None, t))
        events = await _capped_pages(lambda t: self._github.timeline(repo, n, t))
        children = await self._github.sub_issues(repo, n) if issue.sub_issue_count > 0 else None
        now = self._clock()
        self._store.replace_details(repo, n, comments, events, now)
        if children is not None:
            self._store.set_sub_issue_links(repo, n, children, now)
