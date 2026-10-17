# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue (spec §2): synced from GitHub into ``review.sqlite``, served from it.

``list`` never waits for GitHub ("Always cached"): it reads the cache and reports a sync in
progress or a failed one in the view. ``sync`` builds the whole refresh before writing any
of it, so a sync that fails half-way (GitHub unreachable, rate limited) leaves the cache as
it was.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from jesinia.apps.review.application.ports import PullRequests, QueueStore
from jesinia.apps.review.domain.gates import GateInputError
from jesinia.apps.review.domain.queue import (
    PrHeadMovedError,
    PrNotInQueueError,
    PrNotOpenError,
    PrRef,
    PrSnapshot,
    QueueDomainError,
    QueueFilter,
    QueueRow,
    QueueView,
    RepoConfig,
    StoredPr,
    parse_pr_ref,
    queue_order,
    queue_row,
    snapshot_of,
)
from jesinia.apps.review.domain.workspace import check_sha
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubRequestError,
)
from jesinia.services.github.domain.pr import (
    InvalidPullRequestReferenceError,
    PrFile,
    PullRequest,
)

log = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(minutes=2)
# Per-PR detail fetches (checks and reviews) in flight at once during a sync.
DETAIL_CONCURRENCY = 8

Clock = Callable[[], datetime]
OnChange = Callable[[], Awaitable[None]]
# What one PR's sync produced: a fresh snapshot, or the cause it couldn't be loaded.
_Outcome = tuple[PrSnapshot, str | None]


class _Refresh:
    """A sync's result, written to the store in one transaction."""

    def __init__(self) -> None:
        self.fresh: list[PrSnapshot] = []
        self.failed: list[tuple[PrSnapshot, str]] = []
        self.removed: list[PrRef] = []
        self.loaded: set[PrRef] = set()  # loaded when the sync began

    def add(self, outcome: _Outcome) -> None:
        snapshot, error = outcome
        if error is None:
            self.fresh.append(snapshot)
        else:
            self.failed.append((snapshot, error))


class QueueService:
    """The queue's use cases.

    Args:
        prs: GitHub reads (``PrClient``).
        store: ``review.sqlite``.
        clock: the current UTC time.
        on_change: awaited after a sync or a load changed the queue (the scheduler's tick).
    """

    def __init__(
        self,
        prs: PullRequests,
        store: QueueStore,
        clock: Clock,
        *,
        on_change: OnChange | None = None,
    ) -> None:
        self._prs = prs
        self._store = store
        self._clock = clock
        self._on_change = on_change
        self._sync_lock = asyncio.Lock()
        self._syncing = False
        self._backoff_until: datetime | None = None
        self._fetched_during_sync: set[PrRef] = set()
        self._detail_slots = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def list(self, queue_filter: QueueFilter) -> QueueView:
        """The cached queue in priority order; never awaits GitHub or a running sync."""
        viewer = self._store.viewer()
        latest = self._store.latest_rounds()
        stored = [
            pr
            for pr in self._store.prs(include_dismissed=queue_filter.include_dismissed)
            if queue_filter.repo is None or pr.pr.ref.repo == queue_filter.repo
        ]
        stored.sort(key=queue_order)
        synced_at, sync_error = self._store.sync_status()
        return QueueView(
            rows=[queue_row(pr, latest.get(pr.pr.ref), viewer) for pr in stored],
            synced_at=synced_at,
            syncing=self._syncing,
            sync_error=sync_error,
        )

    async def sync(self) -> None:
        """Refresh the queue from GitHub (the ``review.sync`` job, every ``SYNC_INTERVAL``).

        First it watches every repo with a PR awaiting the author's review or written by
        them, unless the user unwatched it. A failure is never raised: it is recorded as the
        view's ``sync_error`` and the cache is left as it was. After a rate limit nothing is
        asked until it resets (or for ``SYNC_INTERVAL`` when GitHub gave no reset time). A
        sync already running makes this one a no-op. The scheduler ticks after every sync,
        failed or not, so debounces still run out on cached data.
        """
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            await self._sync_once(self._clock())
        await self._changed()

    async def _sync_once(self, now: datetime) -> None:
        if self._backoff_until is not None and now < self._backoff_until:
            return
        self._syncing = True
        self._fetched_during_sync = set()
        try:
            refresh = await self._fetch()
        except GitHubRateLimitedError as exc:
            self._backoff_until = exc.reset_at or now + SYNC_INTERVAL
            self._record_failure(exc)
            return
        except (
            GitHubError,
            InvalidPullRequestReferenceError,
            QueueDomainError,
            GateInputError,
        ) as exc:
            self._record_failure(exc)
            return
        finally:
            self._syncing = False
        self._backoff_until = None
        # A PR loaded while GitHub was being read was fetched later than this sync's data:
        # keep the newer row, and keep it even if it left the inbox meanwhile.
        newer = self._fetched_during_sync
        loaded_since = {pr.pr.ref for pr in self._store.prs(include_dismissed=True) if pr.loaded}
        removed = [ref for ref in refresh.removed if ref not in loaded_since - refresh.loaded]
        self._store.apply_sync(
            [pr for pr in refresh.fresh if pr.ref not in newer],
            [(pr, error) for pr, error in refresh.failed if pr.ref not in newer],
            removed,
            now,
        )
        self._store.record_sync(now, None)

    def _record_failure(self, exc: Exception) -> None:
        cause = f"GitHub rate limit: {exc}" if isinstance(exc, GitHubRateLimitedError) else str(exc)
        log.warning("review.sync failed, serving the cached queue: %s", cause)
        self._store.record_sync(None, cause)

    async def _fetch(self) -> _Refresh:
        cached = {pr.pr.ref: pr for pr in self._store.prs(include_dismissed=True)}
        refresh = _Refresh()
        viewer = await self._prs.viewer()
        self._store.set_viewer(viewer)
        if seeded := self._store.seed_repos(await self._prs.inbox_repos()):
            log.info("review: now watching %s", ", ".join(seeded))
        watched = self._store.watched_repos()
        if not watched and not cached:
            return refresh  # nothing to list
        configs = self._store.repo_configs()
        inbox = await self._prs.list_review_inbox(watched) if watched else []
        listed = {PrRef(pr.repo, pr.number) for pr in inbox}
        # Loaded PRs outside the inbox (unwatched repos) stay until they close.
        extra = [ref for ref, pr in cached.items() if pr.loaded and ref not in listed]
        outcomes = await asyncio.gather(
            *(self._outcome(pr, viewer, configs) for pr in inbox),
            *(self._loaded_outcome(cached[ref], viewer, configs) for ref in extra),
        )
        present = [outcome for outcome in outcomes if outcome is not None]
        for outcome in present:
            refresh.add(outcome)
        kept = {snapshot.ref for snapshot, _ in present}
        refresh.removed = [ref for ref in cached if ref not in kept]
        refresh.loaded = {ref for ref, pr in cached.items() if pr.loaded}
        return refresh

    async def _loaded_outcome(
        self, stored: StoredPr, viewer: str, configs: dict[str, RepoConfig]
    ) -> _Outcome | None:
        """A loaded PR's refresh; ``None`` once it is closed (it leaves the queue)."""
        ref = stored.pr.ref
        try:
            pr = await self._prs.get(ref.repo, ref.number)
        except GitHubRateLimitedError:
            raise
        except (GitHubRequestError, GitHubPayloadError) as exc:
            return stored.pr, str(exc)
        if pr.state != "open":
            return None
        return await self._outcome(pr, viewer, configs)

    async def _details(
        self, pr: PullRequest, viewer: str, configs: dict[str, RepoConfig]
    ) -> PrSnapshot:
        """The PR with its CI and reviews; raises whatever GitHub raised."""
        config = configs.get(PrRef(pr.repo, pr.number).repo)
        async with self._detail_slots:
            checks, reviews = await asyncio.gather(
                self._prs.checks(pr.repo, pr.head_sha),
                self._prs.reviews(pr.repo, pr.number),
            )
        return snapshot_of(
            pr,
            ci_state=checks.state,
            reviews=reviews,
            automated_reviewer=None if config is None else config.automated_reviewer,
            viewer=viewer,
        )

    async def _outcome(
        self, pr: PullRequest, viewer: str, configs: dict[str, RepoConfig]
    ) -> _Outcome:
        """The PR's snapshot, or the cause it couldn't be read (with what is known of it,
        used only if the PR isn't cached yet). A rate limit is raised: it is the whole
        sync's problem, not this PR's."""
        error = pr.fetch_error
        if error is None:
            try:
                return await self._details(pr, viewer, configs), None
            except GitHubRateLimitedError:
                raise
            except (
                GitHubRequestError,
                GitHubPayloadError,
                InvalidPullRequestReferenceError,
            ) as exc:
                error = str(exc)
        degraded = snapshot_of(
            pr, ci_state="none", reviews=[], automated_reviewer=None, viewer=viewer
        )
        return degraded, error

    async def load(self, ref: str) -> QueueRow:
        """Load a PR for review now, from a URL, ``owner/repo#n`` or a review route. A cached
        PR needs no GitHub call; it is marked loaded (and undismissed), which puts it first.

        Raises:
            InvalidPrRefError: ``ref`` names no PR.
            PrNotOpenError: GitHub says the PR is closed or merged.
            GitHubError: the PR isn't cached and GitHub couldn't be read.
        """
        pr_ref = parse_pr_ref(ref)
        stored = self._store.set_loaded(pr_ref)
        if stored is None:
            pull = await self._prs.get(pr_ref.repo, pr_ref.number)
            if pull.state != "open":
                raise PrNotOpenError(f"{pr_ref} is {pull.state}; only open PRs can be reviewed")
            viewer = await self._prs.viewer()
            snapshot = await self._details(pull, viewer, self._store.repo_configs())
            stored = self._store.add_loaded(snapshot, self._clock())
            self._fetched_during_sync.add(pr_ref)
        await self._changed()
        latest = self._store.latest_rounds().get(pr_ref)
        return queue_row(stored, latest, self._store.viewer())

    async def dismiss(self, repo: str, number: int) -> None:
        """Hide a PR from the queue until it is loaded again.

        Raises:
            PrNotInQueueError: the PR isn't in the queue.
            InvalidPrRefError: ``repo`` or ``number`` is malformed.
        """
        ref = PrRef(repo, number)
        if not self._store.set_dismissed(ref):
            raise PrNotInQueueError(f"{ref} is not in the review queue")

    async def files(self, repo: str, number: int, head_sha: str) -> builtins.list[PrFile]:
        """The PR's files at ``head_sha``: from the cache, else fetched once and cached.

        GitHub only lists the files of the PR's current head, so the head is read before
        and after the fetch: the files are cached only under a head both reads agree on,
        never under a SHA they weren't fetched for.

        Raises:
            InvalidShaError: ``head_sha`` is not a full commit SHA.
            PrHeadMovedError: the PR's head is no longer ``head_sha`` (or moved mid-fetch).
            GitHubError: not cached and GitHub couldn't be read.
        """
        ref = PrRef(repo, number)
        cached = self._store.pr_files(ref, check_sha(head_sha))
        if cached is not None:
            return cached
        before = await self._prs.get(ref.repo, ref.number)
        files = await self._prs.files(ref.repo, ref.number)
        after = await self._prs.get(ref.repo, ref.number)
        if before.head_sha != after.head_sha:
            raise PrHeadMovedError(f"{ref} was pushed to while its files were read; retry")
        self._store.put_pr_files(ref, after.head_sha, files)
        if after.head_sha != head_sha:
            raise PrHeadMovedError(f"{ref} is now at {after.head_sha}, not {head_sha}")
        return files

    async def _changed(self) -> None:
        if self._on_change is not None:
            await self._on_change()
