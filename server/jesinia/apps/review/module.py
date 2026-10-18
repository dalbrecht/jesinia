# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Review app module: owns ``review.sqlite``, the queue and its pre-review scheduler,
the repo workspace, the repo tools and the agent jobs. Starts last (after ``agents`` and
``github``)."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.review.api.routes import build_router
from jesinia.apps.review.api.tools import build_review_tools, build_tools
from jesinia.apps.review.application.followup import FollowUps
from jesinia.apps.review.application.jobs import JobRunner
from jesinia.apps.review.application.keep import KeepService
from jesinia.apps.review.application.knowledge import KnowledgeService
from jesinia.apps.review.application.pipeline import Pipeline
from jesinia.apps.review.application.posting import PostingService
from jesinia.apps.review.application.queue import SYNC_INTERVAL, QueueService
from jesinia.apps.review.application.repo_tools import RepoTools
from jesinia.apps.review.application.rereview import Rereviews
from jesinia.apps.review.application.reviews import ReviewReads
from jesinia.apps.review.application.scheduler import (
    PRUNE_INTERVAL,
    PreReviewRequest,
    Scheduler,
    WorkspacePruner,
)
from jesinia.apps.review.application.sessions import ReviewSessionRegistry
from jesinia.apps.review.application.stacks import StackReviews
from jesinia.apps.review.domain.queue import SyncStatus
from jesinia.apps.review.infrastructure.knowledge_store import SqliteKnowledgeStore
from jesinia.apps.review.infrastructure.standards_loader import StandardsLoader
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.infrastructure.workspace import GitWorkspace, github_remote_url
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.idempotency import RETENTION, IdempotencyStore, SqliteIdempotencyStore
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.github.application.prs import PrClient
from jesinia.services.github.application.service import GitHubService
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.application.tags import TagService

log = logging.getLogger(__name__)

# History: rounds, drafts, posted reviews and the knowledge model can't be rebuilt from
# GitHub, and `jesinia backup` copies only history databases.
REVIEW_DB = ModuleDb("review", DbClass.HISTORY, files("jesinia.apps.review.migrations"))
SYNC_JOB = "review.sync"
PRUNE_JOB = "review.prune"
IDEMPOTENCY_PRUNE_JOB = "review.idempotency-prune"
RECONCILE_JOB = "review.reconcile-postings"
KNOWLEDGE_JOB = "review.knowledge-ingest"
# How often the knowledge model reads its signals again (each source once, incrementally).
KNOWLEDGE_INTERVAL = timedelta(minutes=30)
# How often postings a crash or a lost response left unsettled are checked against GitHub.
RECONCILE_INTERVAL = timedelta(minutes=5)
# How long a post or Done waits for its review page before answering; the keep goes on in
# the background past that (the PWA reads the round's ``kept`` record when it re-reads).
KEEP_WAIT_SECONDS = 10.0
# How long stop() waits for keeps in flight before cancelling them (N2).
KEEP_STOP_SECONDS = 15.0
# The per-install secret the knowledge model's emails digest is keyed with (M3).
KNOWLEDGE_KEY_FILE = "knowledge.key"


def _not_started() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "review module not started")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _log_keep_failure(round_id: int, task: asyncio.Task[object]) -> None:
    """A keep's own errors are caught inside it; anything else is logged, never lost."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("review.keep round=%d failed in the background: %s", round_id, exc,
                    exc_info=exc)  # fmt: skip


def _knowledge_key(data_dir: Path) -> bytes:
    """The per-install secret for the emails digest (M3): made once, readable by the user
    only."""
    path = data_dir / "review" / KNOWLEDGE_KEY_FILE
    if path.is_file():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(key)
    return key


class ReviewModule:
    name = "review"

    def __init__(self, remote_url: Callable[[str], str] | None = None) -> None:
        # Where a repo is fetched from: GitHub (None), except for the e2e fixture app, which
        # fetches from local repositories beside its fake GitHub.
        self._remote_url = remote_url
        self._workspace: GitWorkspace | None = None
        self._conn: sqlite3.Connection | None = None
        self._repo_tools: RepoTools | None = None
        self._queue: QueueService | None = None
        self._scheduler: Scheduler | None = None
        self._pruner: WorkspacePruner | None = None
        self._jobs: JobRunner | None = None
        self._pipeline: Pipeline | None = None
        self._posting: PostingService | None = None
        self._reviews: ReviewReads | None = None
        self._idempotency: SqliteIdempotencyStore | None = None
        self._followups: FollowUps | None = None
        self._stacks: StackReviews | None = None
        self._knowledge: KnowledgeService | None = None
        self._keeper: KeepService | None = None
        self._keeps: set[asyncio.Task[object]] = set()
        self._stopping = False
        # Review jobs bind their agent sessions here; the repo tools read through it.
        self.sessions = ReviewSessionRegistry()

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            raise _not_started()
        return self._workspace

    @property
    def repo_tools(self) -> RepoTools:
        if self._repo_tools is None:
            raise _not_started()
        return self._repo_tools

    @property
    def queue(self) -> QueueService:
        if self._queue is None:
            if self._scheduler is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "the review queue requires the github service",
                )
            raise _not_started()
        return self._queue

    @property
    def jobs(self) -> JobRunner:
        """Runs review agent jobs (needs the agents service)."""
        if self._jobs is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "review jobs require the agents service",
                )
            raise _not_started()
        return self._jobs

    @property
    def pipeline(self) -> Pipeline:
        """Pre-reviews and opening PRs (needs the agents and github services)."""
        if self._pipeline is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "reviews require the agents service and the github service",
                )
            raise _not_started()
        return self._pipeline

    @property
    def posting(self) -> PostingService:
        """Drafts and posting (needs the github service)."""
        if self._posting is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "posting reviews requires the github service",
                )
            raise _not_started()
        return self._posting

    @property
    def stacks(self) -> StackReviews:
        """Stack reviews and Post all (needs the agents and github services)."""
        if self._stacks is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "stack reviews require the agents service and the github service",
                )
            raise _not_started()
        return self._stacks

    @property
    def reviews(self) -> ReviewReads:
        """Cached review reads and Settings → Review."""
        if self._reviews is None:
            raise _not_started()
        return self._reviews

    @property
    def knowledge(self) -> KnowledgeService:
        """The knowledge model (spec §6.1)."""
        if self._knowledge is None:
            raise _not_started()
        return self._knowledge

    @property
    def keeper(self) -> KeepService:
        """Kept review pages and How it works (needs the kb service)."""
        if self._keeper is None:
            if self._workspace is not None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "keeping review pages requires the kb service",
                )
            raise _not_started()
        return self._keeper

    def _start_keep(self, round_id: int) -> asyncio.Task[object] | None:
        """Start keeping a round's KB page, tracked so stop() can wait for it; none without
        the KB or once stopping (N2). A failure that lands after nobody waits is logged."""
        if self._keeper is None or self._stopping:
            return None
        task: asyncio.Task[object] = asyncio.create_task(self._keeper.keep_finished(round_id))
        self._keeps.add(task)
        task.add_done_callback(self._keeps.discard)
        task.add_done_callback(lambda done: _log_keep_failure(round_id, done))
        return task

    async def keep_in_background(self, round_id: int) -> None:
        """Hand a posted round on to be kept, without waiting (M5: a stack's Post all posts
        PR after PR, and must not wait on the KB for each)."""
        self._start_keep(round_id)

    async def keep_finished(self, round_id: int) -> None:
        """Keep a finished deep round's KB page (spec §8): best effort, so Done never fails
        on it, and bounded, so it never waits on a slow KB for more than
        ``KEEP_WAIT_SECONDS`` (the keep then finishes in the background). Without the KB
        nothing is kept."""
        task = self._start_keep(round_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), KEEP_WAIT_SECONDS)
        except TimeoutError:
            log.info("review.keep round=%d goes on in the background", round_id)

    async def ingest_knowledge(self) -> None:
        """Read the knowledge model's signals (the ``review.knowledge-ingest`` job); failures
        are logged and reported, never raised."""
        if self._knowledge is not None:
            report = await self._knowledge.ingest()
            log.info(
                "knowledge ingest: %d commits, %d reviews, %d quizzes, %d errors",
                report.commits, report.reviews, report.quizzes, len(report.errors),
            )  # fmt: skip

    @property
    def idempotency(self) -> IdempotencyStore:
        if self._idempotency is None:
            raise _not_started()
        return self._idempotency

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys older than ``RETENTION`` (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired review idempotency keys", removed)

    async def _prereview(self, request: PreReviewRequest) -> None:
        await self.pipeline.prereview(request)

    @property
    def scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise _not_started()
        return self._scheduler

    async def sync_queue(self) -> None:
        """Refresh the queue from GitHub (the ``review.sync`` job); the scheduler ticks after."""
        if self._queue is not None:
            await self._queue.sync()

    async def prune_workspace(self) -> None:
        """Drop worktrees nothing needs (the daily ``review.prune`` job)."""
        if self._pruner is not None:
            await self._pruner.run()

    async def reconcile_postings(self) -> None:
        """Settle postings left ``started`` or ``uncertain`` (the ``review.reconcile-postings``
        job, also at start: M3)."""
        if self._posting is not None:
            settled = await self._posting.reconcile_unsettled()
            if settled:
                log.info("settled %d review postings left unsettled", settled)

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return [*build_tools(self), *build_review_tools(self)]

    async def start(self, ctx: AppContext) -> None:
        self._stopping = False
        self._conn = open_db(ctx.cfg.data_dir, REVIEW_DB)
        self._workspace = GitWorkspace(
            ctx.cfg.data_dir / "review" / "repos", self._remote_url or github_remote_url
        )
        self._repo_tools = RepoTools(
            self.sessions.worktree_for,
            self._workspace,
            self.sessions.allowance,
            git_slots=self.sessions.git_slot,
            tree_key=self._workspace.tree_key,
            reads=self.sessions.reads,
            budget=self.sessions.budget,
        )
        self.sessions.use_tree_locks(self._workspace.tree_lock)
        store = SqliteReviewStore(self._conn)
        self._idempotency = SqliteIdempotencyStore(self._conn)
        # Single process and nothing served yet: any pending key was left by a crash.
        if cleared := self._idempotency.clear_pending():
            log.info("cleared %d review idempotency keys left pending by an unclean stop", cleared)
        ctx.scheduler.every(
            IDEMPOTENCY_PRUNE_JOB, timedelta(days=1), self.prune_idempotency_keys, at_start=True
        )

        def cached_status() -> SyncStatus:  # without GitHub, nothing ever syncs
            synced_at, error = store.sync_status()
            return SyncStatus(synced_at, False, error)

        self._reviews = ReviewReads(store, cached_status)
        agents = ctx.services.get("agents")
        github = ctx.services.get("github")
        kb, tags = ctx.services.get("kb"), ctx.services.get("tags")
        if isinstance(kb, KbService) and isinstance(tags, TagService):
            self._keeper = KeepService(kb=kb, tags=tags, store=store, clock=_utc_now)

        def knowledge_repos() -> list[str]:
            return [*store.watched_repos(), *(stored.pr.ref.repo for stored in store.prs())]

        self._knowledge = KnowledgeService(
            store=SqliteKnowledgeStore(self._conn),
            clock=_utc_now,
            workspace=self._workspace,
            identity=PrClient(github) if isinstance(github, GitHubService) else None,
            repos=knowledge_repos,
            email_key=_knowledge_key(ctx.cfg.data_dir),
        )
        ctx.scheduler.every(KNOWLEDGE_JOB, KNOWLEDGE_INTERVAL, self.ingest_knowledge, at_start=True)
        reviews = isinstance(agents, SessionService) and isinstance(github, GitHubService)
        # The review pipeline runs the pre-reviews; without agents and GitHub the gates are
        # still applied and ready PRs show as queued.
        # The follow-ups run with GitHub: a loaded PR's new head then waits for its check.
        self._scheduler = Scheduler(
            store,
            _utc_now,
            run=self._prereview if reviews else None,
            hold_unchecked=isinstance(github, GitHubService),
        )
        if interrupted := self._scheduler.recover():
            log.info("re-queued %d review rounds left running by a stop", interrupted)
        self._pruner = WorkspacePruner(store, self._workspace, self.sessions.bound_trees)
        ctx.scheduler.every(PRUNE_JOB, PRUNE_INTERVAL, self.prune_workspace, at_start=True)
        if isinstance(agents, SessionService):
            self._jobs = JobRunner(
                agents,
                self._workspace,
                StandardsLoader(self._workspace, store.standards_dir),
                registry=self.sessions,
                store=store,
            )
        if isinstance(github, GitHubService):
            prs = PrClient(github)
            scheduler = self._scheduler
            followups = FollowUps(
                github=prs, workspace=self._workspace, store=store, after=scheduler.tick
            )
            self._followups = followups

            async def changed() -> None:
                await scheduler.tick()
                # New heads are classified in the background (git work), then it ticks again.
                followups.start_refresh()
                # And each stack's heads are checked (R77), so the queue files a stack needing
                # a restack before anyone opens it.
                if self._stacks is not None:
                    self._stacks.start_verify()

            self._queue = QueueService(
                prs, store, _utc_now, on_change=changed, on_refresh=followups.check
            )
            self._reviews = ReviewReads(store, self._queue.status)
            self._posting = PostingService(
                github=prs,
                writer=github,
                files=self._queue,
                store=store,
                clock=_utc_now,
                on_posted=self.keep_in_background,
            )
            ctx.scheduler.every(SYNC_JOB, SYNC_INTERVAL, self.sync_queue, at_start=True)
            ctx.scheduler.every(
                RECONCILE_JOB, RECONCILE_INTERVAL, self.reconcile_postings, at_start=True
            )
            if self._jobs is not None:
                self._pipeline = Pipeline(
                    github=prs,
                    files=self._queue,
                    workspace=self._workspace,
                    jobs=self._jobs,
                    store=store,
                    bus=ctx.bus,
                    clock=_utc_now,
                    slots=self._scheduler,
                    followups=followups,
                    rereviews=Rereviews(github=prs, workspace=self._workspace, store=store),
                    knowledge=self._knowledge,
                    system_notes=self._keeper,
                )
                # A head classified after its round was planned gets its re-review then.
                followups.on_classified = self._pipeline.replan
                self._stacks = StackReviews(
                    store=store,
                    reads=self._reviews,
                    opener=self._pipeline,
                    poster=self._posting,
                    jobs=self._jobs,
                    github=prs,
                    workspace=self._workspace,
                    clock=_utc_now,
                    slots=self._scheduler,
                    bus=ctx.bus,
                    knowledge=self._knowledge,
                )
                # Nothing runs yet: a stack job left running was cut short by a stop (R73).
                if swept := self._stacks.recover():
                    log.info("marked stack jobs of %d stack reviews interrupted", swept)

    async def stop(self) -> None:
        self._stopping = True  # no keep starts from here on (N2)
        if self._keeps:  # keeps in flight get a bounded wait, then are cancelled
            _, pending = await asyncio.wait(set(self._keeps), timeout=KEEP_STOP_SECONDS)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                log.warning("review.keep stop cancelled %d keep(s) still running", len(pending))
        if self._scheduler is not None:
            self._scheduler.pause()  # cancelling an open frees a slot: nothing may take it
        if self._stacks is not None:
            await self._stacks.stop_verify()
            await self._stacks.stop()
        if self._pipeline is not None:
            await self._pipeline.stop()
        if self._scheduler is not None:
            await self._scheduler.stop()
        if self._followups is not None:
            await self._followups.stop()
            self._followups = None
        self._pipeline = None
        self._stacks = None
        self._posting = None
        self._reviews = None
        self._idempotency = None
        self._knowledge = None
        self._keeper = None
        self._scheduler = None
        self._jobs = None
        self._queue = None
        self._pruner = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._workspace = None
        self._repo_tools = None

    def health(self) -> ModuleHealth:
        ready = self._workspace is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
