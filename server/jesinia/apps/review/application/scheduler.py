# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background pre-reviews (spec §2 "Pre-review timing") and the workspace prune job.

``Scheduler.tick`` runs after every sync and load: it applies the gates to each queued PR,
records the pre-review state, and starts the ready ones in priority order (loaded, then
requested, then agent-authored, then the rest), at most ``CONCURRENCY`` at a time. Each run
is a round on the PR's current head, so a head is pre-reviewed once: rapid pushes restart
the debounce and only the head that settles is reviewed. A round a stop or restart cut short
is not the PR's failure: its head is queued again, up to ``MAX_INTERRUPTIONS`` times.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jesinia.apps.review.application.errors import RoundInterruptedError
from jesinia.apps.review.application.ports import PrunableWorkspace, QueueStore
from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.gates import (
    DEFAULT_GATES,
    Depth,
    GateResult,
    GateSettings,
    PrState,
    gate,
)
from jesinia.apps.review.domain.queue import (
    PreReviewMode,
    PreReviewState,
    PrRef,
    RepoConfig,
    RoundStatus,
    StoredPr,
    queue_order,
)
from jesinia.apps.review.domain.workspace import InvalidShaError, check_sha

log = logging.getLogger(__name__)

CONCURRENCY = 2
PRUNE_INTERVAL = timedelta(days=1)
# Interrupted rounds on one head before it counts as failed (so a run that crashes the
# server can't loop forever).
MAX_INTERRUPTIONS = 3
# Loaded PRs are reviewed at once: no debounce, but CI and automated reviews still gate them.
_LOADED_GATES = GateSettings(debounce=timedelta(0))
_STOPPED = "Jesinia stopped during this pre-review"
_INTERRUPTED = "Jesinia stopped during this pre-review (it was restarted or crashed)"


def gate_for(
    stored: StoredPr, config: RepoConfig | None, now: datetime, *, immediate: bool
) -> GateResult:
    """The gates on a cached PR's head. ``immediate``: the author asked for it now (loaded
    or opened), so there is no debounce; CI and automated reviews still hold it back."""
    pr = stored.pr
    state = PrState(
        ci_state=pr.ci_state,
        automated_reviewer=None if config is None else config.automated_reviewer,
        auto_review_landed=pr.auto_review_state == "landed",
        last_head_change_at=stored.last_head_change_at,
        fetch_error=stored.fetch_error,
    )
    return gate(state, now, _LOADED_GATES if immediate else DEFAULT_GATES)


class SchedulerConfigError(ReviewError, ValueError):
    """The scheduler was configured with impossible values."""


@dataclass(frozen=True)
class PreReviewRequest:
    """One background pre-review: the round it fills and what to run (per the setting)."""

    round_id: int
    ref: PrRef
    display_repo: str
    head_sha: str
    base_sha: str
    depth: Depth
    mode: PreReviewMode
    loaded: bool


PreReviewRunner = Callable[[PreReviewRequest], Awaitable[None]]
Clock = Callable[[], datetime]


class Scheduler:
    """Runs pre-reviews for queued PRs.

    Args:
        store: ``review.sqlite``.
        clock: the current UTC time.
        run: the pre-review itself (the review pipeline). Without one, the gates are still
            applied and ready PRs show as queued, but nothing runs.
        concurrency: pre-reviews running at once.
    """

    def __init__(
        self,
        store: QueueStore,
        clock: Clock,
        *,
        run: PreReviewRunner | None = None,
        concurrency: int = CONCURRENCY,
    ) -> None:
        if concurrency < 1:
            raise SchedulerConfigError(f"concurrency must be at least 1, got {concurrency}")
        self._store = store
        self._clock = clock
        self._run = run
        self._concurrency = concurrency
        self._pending: dict[PrRef, tuple[StoredPr, PreReviewMode]] = {}
        self._running: dict[PrRef, asyncio.Task[None]] = {}
        # Rounds the author opened share the slots (R52), ahead of queued pre-reviews.
        self._open_waiters: deque[asyncio.Future[None]] = deque()
        self._opens_running = 0

    @property
    def runs_pre_reviews(self) -> bool:
        """Whether a runner is set (without one, ready PRs only show as queued)."""
        return self._run is not None

    def recover(self) -> int:
        """Mark rounds a crash left unfinished as interrupted, so the next tick queues their
        heads again (call once at start, before any tick)."""
        return self._store.interrupt_unfinished_rounds(self._clock(), _INTERRUPTED)

    async def tick(self) -> None:
        """Apply the gates to every queued PR and start what is ready and has a free slot."""
        now = self._clock()
        configs = self._store.repo_configs()
        default_mode = self._store.prereview_default()
        rounds = self._store.rounds_for_heads()
        self._pending = {}
        with self._store.atomic():  # one write per tick, and none for unchanged states
            for stored in self._store.prs():
                ref, head = stored.pr.ref, stored.pr.head_sha
                if ref in self._running:
                    continue
                status = rounds.get((ref, head))
                if status is None or _retryable(status):
                    self._consider(stored, now, configs.get(ref.repo), default_mode)
                elif status.finished:
                    # This head had its pre-review; show how it ended.
                    self._store.set_state(ref, head, *_outcome(status))
        self._dispatch()

    def _consider(
        self,
        stored: StoredPr,
        now: datetime,
        config: RepoConfig | None,
        default_mode: PreReviewMode,
    ) -> None:
        pr = stored.pr
        result = gate_for(stored, config, now, immediate=stored.loaded)
        if result.state != "ready":
            self._store.set_state(pr.ref, pr.head_sha, result.state, result.reason)
            return
        if config is not None and config.suggested and not stored.loaded:
            # Someone else's repo the author hasn't confirmed: nothing runs on its code.
            reason = "suggested repo: confirm it to pre-review its PRs"
            self._store.set_state(pr.ref, pr.head_sha, "not_started", reason)
            return
        mode = self._mode(config, default_mode)
        if mode == "on_open" and not stored.loaded:
            reason = "pre-review is set to run when you open the PR"
            self._store.set_state(pr.ref, pr.head_sha, "not_started", reason)
            return
        self._store.set_state(pr.ref, pr.head_sha, "queued", result.reason)
        self._pending[pr.ref] = (stored, mode)

    @staticmethod
    def _mode(config: RepoConfig | None, default_mode: PreReviewMode) -> PreReviewMode:
        if config is not None and config.prereview is not None:
            return config.prereview
        return default_mode

    async def run_open[T](self, work: Callable[[], Awaitable[T]]) -> T:
        """Run an opened PR's round in one of the scheduler's slots (R52): at most
        ``concurrency`` rounds run at once, pre-reviews and opens together. An open waits
        for a free slot and goes before any queued pre-review."""
        if self._open_waiters or self._busy() >= self._concurrency:
            waiter = asyncio.get_running_loop().create_future()
            self._open_waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter.done() and not waiter.cancelled():  # granted as it was cancelled
                    self._release_open()
                elif waiter in self._open_waiters:
                    self._open_waiters.remove(waiter)
                raise
        else:
            self._opens_running += 1
        try:
            return await work()
        finally:
            self._release_open()

    def _release_open(self) -> None:
        self._opens_running -= 1
        self._dispatch()

    def _busy(self) -> int:
        return len(self._running) + self._opens_running

    def _dispatch(self) -> None:
        while self._open_waiters and self._busy() < self._concurrency:
            waiter = self._open_waiters.popleft()
            if not waiter.done():  # a cancelled waiter wants no slot
                self._opens_running += 1
                waiter.set_result(None)
        if self._run is None:
            return
        while self._pending and self._busy() < self._concurrency:
            stored, mode = min(self._pending.values(), key=lambda entry: queue_order(entry[0]))
            del self._pending[stored.pr.ref]
            if self._still_queued(stored):
                self._start(stored, mode, self._run)

    def _still_queued(self, stored: StoredPr) -> bool:
        """A PR queued at the last tick may since have moved, failed to load, been dismissed
        or been opened (a round now on its head); the next tick decides about it then."""
        now = self._store.pr(stored.pr.ref)
        status = self._store.rounds_for_heads().get((stored.pr.ref, stored.pr.head_sha))
        return (
            now is not None
            and not now.dismissed
            and now.fetch_error is None
            and now.pr.head_sha == stored.pr.head_sha
            and (status is None or _retryable(status))
        )

    def _start(self, stored: StoredPr, mode: PreReviewMode, run: PreReviewRunner) -> None:
        pr = stored.pr
        round_id = resume_or_start_round(self._store, stored, self._clock())
        request = PreReviewRequest(
            round_id=round_id,
            ref=pr.ref,
            display_repo=pr.display_repo,
            head_sha=pr.head_sha,
            base_sha=pr.base_sha,
            depth=pr.depth,
            mode=mode,
            loaded=stored.loaded,
        )
        self._store.set_state(pr.ref, pr.head_sha, "running", f"pre-review ({mode})")
        task = asyncio.create_task(self._execute(request, run), name=f"review:{pr.ref}")
        self._running[pr.ref] = task

    async def _execute(self, request: PreReviewRequest, run: PreReviewRunner) -> None:
        ref, head = request.ref, request.head_sha
        try:
            await run(request)
        except asyncio.CancelledError:
            self._store.finish_round(request.round_id, self._clock(), _STOPPED, interrupted=True)
            self._store.set_state(ref, head, "queued", "interrupted by a stop; it runs again")
            raise
        except RoundInterruptedError as exc:
            # A routine push or a GitHub hiccup: not the PR's failure, and not counted.
            self._store.finish_round(
                request.round_id, self._clock(), str(exc), interrupted=True, counted=False
            )
            self._store.set_state(ref, head, "queued", str(exc))
        except Exception as exc:  # one PR's failure must not stop the queue; its cause is kept
            cause = str(exc) or type(exc).__name__
            log.warning("review pre-review of %s failed: %s", ref, cause, exc_info=exc)
            self._store.finish_round(request.round_id, self._clock(), cause)
            self._store.set_state(ref, head, "failed", f"pre-review failed: {cause}")
        else:
            self._store.finish_round(request.round_id, self._clock(), None)
            self._store.set_state(ref, head, "ready", "pre-review finished")
        finally:
            self._running.pop(ref, None)
        self._dispatch()

    async def drain(self) -> None:
        """Wait until nothing is running (including runs started by finishing ones)."""
        while self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def stop(self) -> None:
        """Cancel running pre-reviews; their rounds are marked failed (retry later)."""
        self._pending = {}
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _retryable(status: RoundStatus) -> bool:
    """A round a stop cut short leaves its head to be queued again, a few times."""
    return status.interrupted and status.interruptions < MAX_INTERRUPTIONS


def resume_or_start_round(store: QueueStore, stored: StoredPr, now: datetime) -> int:
    """The round to run on ``stored``'s head: the interrupted one, resumed with its finished
    jobs and drafts (R51), or a new one."""
    pr = stored.pr
    latest = store.latest_round_on_head(pr.ref, pr.head_sha)
    if latest is not None and _retryable(latest[1]) and store.resume_round(latest[0]):
        return latest[0]
    return store.start_round(pr.ref, pr.head_sha, pr.base_sha, pr.depth, now)


def _outcome(status: RoundStatus) -> tuple[PreReviewState, str]:
    if status.error is None:
        return "ready", "pre-review finished"
    if status.interrupted:
        cause = f"interrupted {status.interruptions} times: {status.error}"
        return "failed", f"pre-review failed: {cause}"
    return "failed", f"pre-review failed: {status.error}"


class WorkspacePruner:
    """The daily ``review.prune`` job: drop worktrees and kept commits nothing needs.

    Kept: the head and base of every PR in the queue (not dismissed), of rounds still in
    review, and the tree of every live review session (a running agent reads it).
    """

    def __init__(
        self,
        store: QueueStore,
        workspace: PrunableWorkspace,
        bound_trees: Callable[[], Iterable[Path]],
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._bound_trees = bound_trees

    async def run(self) -> int:
        removed = await self._workspace.prune(self._keep)
        log.info("review.prune removed %d worktrees", removed)
        return removed

    async def _keep(self, repo: str) -> set[str]:
        """``repo``'s keep set, read fresh: the workspace asks inside the repo's lock."""
        keep = {sha for name, sha in self._store.keep_shas() if name == repo and _is_sha(sha)}
        for tree in self._bound_trees():
            key = self._workspace.tree_key(tree)
            if key is not None and key[0] == repo:
                keep.add(key[1])
        return keep


def _is_sha(sha: str) -> bool:
    try:
        check_sha(sha)
    except InvalidShaError:
        return False
    return True
