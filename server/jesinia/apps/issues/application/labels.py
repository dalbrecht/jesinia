# SPDX-License-Identifier: AGPL-3.0-or-later
"""The label setup and migration batches (spec §2, §6.2): planned, previewed, then applied with
the author's approval through ``LabelExecutor``.

``LabelExecutor`` is the minimal ordered executor for ``LabelOp`` (ruling IR12). Task 6's apply
engine reuses it for its ``EnsureLabel`` steps and inherits its pacing. It:

- starts from a fresh read of the repo's labels (IR4), so a label is created only by a
  ``CreateLabel`` step, and a move never asks GitHub to add a label the repo lacks (GitHub would
  create it silently);
- moves a label's members from a **live** listing, never the cache (IR31). For each
  ``DeleteLabel`` with a ``replacement`` it first lists every issue and pull request carrying
  the old label and moves each to the replacement: add it, then remove the old label in GitHub's
  current spelling. This happens for every such delete before any delete runs. Then, right
  before each delete, it lists the label again and moves anything added since. A delete runs only
  when no step of the batch has failed: if any move failed, nothing is deleted, and the result
  names the issues that failed;
- never deletes a label whose name has a comma: GitHub's ``labels=`` filter can't list it, so its
  members can't be known (left for the author to delete by hand);
- paces its writes, ``WRITE_INTERVAL`` apart (GitHub's secondary limits ask for about a second
  between mutating requests); reads aren't paced;
- stops the whole batch at a rate limit (IR5) or when GitHub becomes unavailable (a rejected
  token), marking the rest skipped. A rate limit returns GitHub's reset time, so the route can
  answer with ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Literal

from jesinia.apps.issues.application.ports import LabelPlanStore, LabelWrites, RepoState
from jesinia.apps.issues.domain.labels import (
    CreateLabel,
    DeleteLabel,
    LabelOp,
    LabelPlan,
    LabelPlanKind,
    LabelPlanNotFoundError,
    MigrationPlan,
    migration_aliases,
    plan_migration,
    plan_setup,
)
from jesinia.apps.issues.domain.schema import label_key
from jesinia.apps.issues.domain.triage import RepoNotWatchedError, triage
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.issue import IssueDomainError, Label, LabeledItem
from jesinia.services.github.domain.repo import validate_repo

log = logging.getLogger(__name__)

StepState = Literal["done", "failed", "skipped"]
Clock = Callable[[], datetime]
Pause = Callable[[float], Awaitable[None]]
# Told GitHub's reset time when GitHub rate limited a batch (the sync pauses until then).
OnRateLimit = Callable[[datetime | None], None]

WRITE_INTERVAL = timedelta(seconds=1)
COMMA_CAUSE = (
    "not deleted: GitHub can't list the issues carrying a label whose name has a comma, so "
    "they can't be moved first; delete it by hand after checking"
)


@dataclass(frozen=True)
class OpResult:
    """One op's outcome. ``cause`` says why it failed or was skipped (or a no-op's reason);
    ``failed_issues`` are the issues or pull requests a delete's moves failed on."""

    op: LabelOp
    state: StepState
    cause: str | None = None
    failed_issues: tuple[int, ...] = ()


@dataclass(frozen=True)
class LabelRun:
    """A batch's outcome, op by op, in order.

    ``is_rate_limited`` means GitHub's rate limit stopped the batch (``rate_limited_until`` is
    its reset time, when it gave one); ``stopped`` is the cause whenever the batch stopped early;
    ``note`` says why old labels were kept, if they were. ``labels`` are the repo's labels after
    the run and ``relabelled`` each moved issue's (or pull request's) labels after it, for the
    cache.
    """

    results: list[OpResult]
    is_rate_limited: bool = False
    rate_limited_until: datetime | None = None
    note: str | None = None
    stopped: str | None = None
    labels: list[Label] = field(default_factory=list)
    relabelled: dict[int, tuple[str, ...]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return all(result.state == "done" for result in self.results)


class _StepFailedError(Exception):
    """An op that can't run, with the cause to report (internal to the executor)."""

    def __init__(self, cause: str, failed_issues: tuple[int, ...] = ()) -> None:
        super().__init__(cause)
        self.failed_issues = failed_issues


class _BatchStoppedError(Exception):
    """GitHub stopped the batch (a rate limit or a rejected token)."""

    def __init__(self, cause: GitHubError) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _Run:
    """One executor run's state."""

    def __init__(self, github: LabelWrites, pause: Pause, repo: str, labels: Sequence[Label]):
        self.github = github
        self.pause = pause
        self.repo = repo
        self.known: dict[str, Label] = {label_key(label.name): label for label in labels}
        self.relabelled: dict[int, tuple[str, ...]] = {}
        self.has_written = False

    async def write[T](self, call: Callable[[], Awaitable[T]]) -> T:
        if self.has_written:
            await self.pause(WRITE_INTERVAL.total_seconds())
        self.has_written = True
        try:
            return await call()
        except (GitHubRateLimitedError, GitHubUnavailable) as exc:
            raise _BatchStoppedError(exc) from exc

    async def create(self, op: CreateLabel) -> str | None:
        if label_key(op.name) in self.known:
            return "already on GitHub"
        created = await self.write(
            lambda: self.github.create_label(self.repo, op.name, op.color, op.description)
        )
        self.known[label_key(created.name)] = created
        return None

    async def members(self, name: str) -> list[LabeledItem]:
        try:
            return await self.github.labeled(self.repo, name)
        except (GitHubRateLimitedError, GitHubUnavailable) as exc:
            raise _BatchStoppedError(exc) from exc

    async def move(self, op: DeleteLabel, replacement: str) -> None:
        """Move every live member of ``op.name`` to ``replacement``.

        Raises:
            _StepFailedError: the replacement doesn't exist, or some moves failed (the rest
                were still made).
        """
        target = self.known.get(label_key(replacement))
        if target is None:
            raise _StepFailedError(
                f"{replacement!r} doesn't exist on GitHub, and Jesinia never creates labels "
                "implicitly; run the label setup first"
            )
        failures: list[tuple[int, str]] = []
        for item in await self.members(op.name):
            try:
                await self._move_one(item, op.name, target.name)
            except (GitHubError, IssueDomainError) as exc:
                failures.append((item.number, str(exc)))
        if failures:
            causes = "; ".join(f"#{number}: {cause}" for number, cause in failures)
            raise _StepFailedError(
                f"{op.name!r} was not deleted: moving it to {replacement!r} failed on {causes}",
                tuple(number for number, _ in failures),
            )

    async def _move_one(self, item: LabeledItem, old: str, new: str) -> None:
        current = {label_key(label): label for label in item.labels}
        labels: tuple[str, ...] = item.labels
        if label_key(new) not in current:
            labels = await self.write(lambda: self.github.add_labels(self.repo, item.number, [new]))
        spelled = current.get(label_key(old), old)  # GitHub listed it as carrying ``old``
        labels = await self.write(lambda: self.github.remove_label(self.repo, item.number, spelled))
        if not item.is_pull_request:
            self.relabelled[item.number] = labels

    async def delete(self, op: DeleteLabel) -> str | None:
        cause: str | None = None
        try:
            await self.write(lambda: self.github.delete_label(self.repo, op.name))
        except GitHubRequestError as exc:
            if exc.status != 404:
                raise
            cause = "already gone"
        self.known.pop(label_key(op.name), None)
        return cause


class LabelExecutor:
    """Runs ``LabelOp`` in order against one repo (see the module docstring).

    Args:
        github: label reads and writes (``IssueClient``).
        pause: awaited ``WRITE_INTERVAL`` between writes (``asyncio.sleep``).
    """

    def __init__(self, github: LabelWrites, *, pause: Pause = asyncio.sleep) -> None:
        self._github = github
        self._pause = pause

    async def run(
        self, repo: str, ops: Sequence[LabelOp], labels: Sequence[Label] | None = None
    ) -> LabelRun:
        """Run ``ops`` on ``repo``. ``labels`` is a read of the repo's labels made just now
        (read here when not given).

        Raises:
            GitHubError: reading the repo's labels failed (nothing ran).
        """
        current = list(labels) if labels is not None else await self._github.labels(repo)
        run = _Run(self._github, self._pause, repo, current)
        results: dict[int, OpResult] = {}
        at = 0  # the op running now, for the report if GitHub stops the batch
        try:
            # Creates and moves first, in order; deletes only after every move was made.
            for index, op in enumerate(ops):
                at = index
                if isinstance(op, CreateLabel):
                    results[index] = await self._step(op, partial(run.create, op))
                elif op.replacement is not None and "," in op.name:
                    results[index] = OpResult(op, "skipped", COMMA_CAUSE)
                elif op.replacement is not None:
                    failed = await self._step(op, partial(self._moved, run, op, op.replacement))
                    if failed.state == "failed":
                        results[index] = failed
            note = self._note(results)
            for index, op in enumerate(ops):
                if index in results or not isinstance(op, DeleteLabel):
                    continue
                if note is not None:
                    results[index] = OpResult(op, "skipped", note)
                    continue
                at = index
                results[index] = await self._step(op, partial(self._delete, run, op))
                note = self._note(results)
        except _BatchStoppedError as stopped:
            return self._stopped(ops, results, run, stopped.cause, at)
        return LabelRun(
            [results[i] for i in range(len(ops))],
            note=self._note(results),
            labels=list(run.known.values()),
            relabelled=run.relabelled,
        )

    @staticmethod
    async def _moved(run: _Run, op: DeleteLabel, replacement: str) -> str | None:
        await run.move(op, replacement)
        return None

    @staticmethod
    async def _delete(run: _Run, op: DeleteLabel) -> str | None:
        if op.replacement is not None:
            await run.move(op, op.replacement)  # anything labelled since the first pass
        return await run.delete(op)

    @staticmethod
    async def _step(op: LabelOp, work: Callable[[], Awaitable[str | None]]) -> OpResult:
        try:
            cause = await work()
        except _StepFailedError as exc:
            return OpResult(op, "failed", str(exc), exc.failed_issues)
        except (GitHubError, IssueDomainError) as exc:
            return OpResult(op, "failed", str(exc))
        return OpResult(op, "done", cause)

    @staticmethod
    def _note(results: dict[int, OpResult]) -> str | None:
        failed = sum(1 for result in results.values() if result.state == "failed")
        if not failed:
            return None
        return (
            f"Nothing was deleted: {failed} step{'s' if failed > 1 else ''} failed, "
            "so the old labels were kept."
        )

    @staticmethod
    def _stopped(
        ops: Sequence[LabelOp],
        results: dict[int, OpResult],
        run: _Run,
        cause: GitHubError,
        at: int,
    ) -> LabelRun:
        """The run after GitHub stopped it at op ``at``: that op failed, the rest without a
        result are skipped."""
        reason = "rate limit" if isinstance(cause, GitHubRateLimitedError) else "unavailability"
        skipped = f"not run: the batch stopped at GitHub's {reason} ({cause})"
        final: list[OpResult] = []
        for index, op in enumerate(ops):
            if index in results:
                final.append(results[index])
            elif index == at:
                final.append(OpResult(op, "failed", str(cause)))
            else:
                final.append(OpResult(op, "skipped", skipped))
        limited = cause if isinstance(cause, GitHubRateLimitedError) else None
        return LabelRun(
            final,
            is_rate_limited=limited is not None,
            rate_limited_until=None if limited is None else limited.reset_at,
            stopped=str(cause),
            labels=list(run.known.values()),
            relabelled=run.relabelled,
        )


class LabelPlanner:
    """Plans a repo's label setup or migration from a fresh read of its labels, its stored
    schema and (for a migration) each alias's members listed live."""

    def __init__(self, github: LabelWrites, store: LabelPlanStore) -> None:
        self._github = github
        self._store = store

    def watched(self, repo: str) -> RepoState:
        """Raises: RepoNotWatchedError: the author hasn't added ``repo``."""
        state = self._store.repo(validate_repo(repo))
        if state is None or not state.watched:
            raise RepoNotWatchedError(f"{repo} is not a watched repo; add it first")
        return state

    async def setup(self, repo: str) -> list[CreateLabel]:
        """The canonical labels the repo is missing.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author hasn't added ``repo``.
            LabelNameCollisionError: two GitHub labels compare as one name.
            GitHubError: GitHub couldn't be read.
        """
        ops, _ = await self.current(repo, "setup")
        return [op for op in ops if isinstance(op, CreateLabel)]

    async def migrate(self, repo: str) -> MigrationPlan:
        """Every alias on GitHub mapped to its canonical label, with its members listed live
        (a name with a comma can't be listed, so it shows none).

        Raises:
            as ``setup``.
        """
        state = self.watched(repo)
        labels = await self._github.labels(state.repo)
        return await self._migration(state, labels)

    async def _migration(self, state: RepoState, labels: Sequence[Label]) -> MigrationPlan:
        names = [label.name for label in labels]
        members: dict[str, list[int]] = {}
        for alias, _ in migration_aliases(state.schema, names):
            if "," not in alias:
                members[alias] = [i.number for i in await self._github.labeled(state.repo, alias)]
        return plan_migration(state.schema, names, members)

    async def current(
        self, repo: str, kind: LabelPlanKind
    ) -> tuple[tuple[LabelOp, ...], list[Label]]:
        """The ops ``kind`` would run now, and the fresh read of the labels they came from.

        Raises:
            as ``setup``.
        """
        state = self.watched(repo)
        labels = await self._github.labels(state.repo)
        if kind == "setup":
            return tuple(plan_setup(state.schema, [label.name for label in labels])), labels
        return tuple((await self._migration(state, labels)).ops()), labels


class LabelSetupService:
    """Previews and applies the label batches. Applying is an approval: the route allows the
    owner only and keys the request.

    Args:
        github: label reads and writes (``IssueClient``).
        store: ``issues.sqlite``.
        clock: the current UTC time.
        on_rate_limit: told GitHub's reset time whenever GitHub rate limits a preview or apply.
        pause: the executor's write pacing (``asyncio.sleep``).
    """

    def __init__(
        self,
        github: LabelWrites,
        store: LabelPlanStore,
        clock: Clock,
        *,
        on_rate_limit: OnRateLimit | None = None,
        pause: Pause = asyncio.sleep,
    ) -> None:
        self.planner = LabelPlanner(github, store)
        self._executor = LabelExecutor(github, pause=pause)
        self._store = store
        self._clock = clock
        self._on_rate_limit: OnRateLimit = on_rate_limit or (lambda until: None)
        self._locks: dict[str, asyncio.Lock] = {}

    def _rate_limited(self, until: datetime | None) -> None:
        self._on_rate_limit(until)

    async def _current(
        self, repo: str, kind: LabelPlanKind
    ) -> tuple[tuple[LabelOp, ...], list[Label]]:
        try:
            return await self.planner.current(repo, kind)
        except GitHubRateLimitedError as exc:
            self._rate_limited(exc.reset_at)
            raise

    async def preview(self, repo: str, kind: LabelPlanKind) -> LabelPlan:
        """Plan the batch and keep it under a new id; nothing is written to GitHub.

        Raises:
            InvalidRepoError, RepoNotWatchedError, LabelNameCollisionError, GitHubError: as the
            planner's.
        """
        name = validate_repo(repo)
        ops, _ = await self._current(name, kind)
        now = self._clock()
        plan = LabelPlan(uuid.uuid4().hex, name, kind, ops, now)
        self._store.save_plan(plan, now)
        return plan

    async def apply(self, repo: str, kind: LabelPlanKind, plan_id: str) -> LabelRun:
        """Apply a previewed plan, once: the plan is consumed whatever happens, and anything
        left to do needs a fresh preview (which plans only what remains). A complete migration
        marks the repo migrated. The cache is updated at once, best effort (IR32).

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            LabelPlanNotFoundError: no such plan for ``repo``.
            LabelPlanKindMismatchError, LabelPlanExpiredError, LabelPlanOutdatedError: the plan
                no longer stands (see ``LabelPlan.check``).
            RepoNotWatchedError, LabelNameCollisionError, GitHubError: as the planner's.
        """
        name = validate_repo(repo)
        async with self._locks.setdefault(name, asyncio.Lock()):
            plan = self._store.take_plan(plan_id, name)
            if plan is None:
                raise LabelPlanNotFoundError(
                    f"no previewed plan {plan_id!r} for {name}; preview the batch first"
                )
            # Kind and expiry first, before asking GitHub; then against the repo as it is now.
            plan.check(kind, self._clock(), plan.ops)
            ops, labels = await self._current(name, kind)
            plan.check(kind, self._clock(), ops)
            run: LabelRun | None = None
            try:
                run = await self._executor.run(name, plan.ops, labels)
            finally:
                self._log(plan, run)
            if run.is_rate_limited:
                self._rate_limited(run.rate_limited_until)
            if kind == "migrate" and run.complete:
                self._store.set_migrated(name)
            self._update_cache(name, run)
            return run

    @staticmethod
    def _log(plan: LabelPlan, run: LabelRun | None) -> None:
        """A structured summary, kept even when the run didn't finish (the response may be
        lost, and the plan is already consumed)."""
        if run is None:
            log.error(
                "issues.labels apply repo=%s kind=%s plan=%s did not finish",
                plan.repo,
                plan.kind,
                plan.id,
            )
            return
        counts = {
            state: sum(r.state == state for r in run.results)
            for state in ("done", "failed", "skipped")
        }
        log.info(
            "issues.labels apply repo=%s kind=%s plan=%s done=%d failed=%d skipped=%d "
            "rate_limited=%s",
            plan.repo,
            plan.kind,
            plan.id,
            counts["done"],
            counts["failed"],
            counts["skipped"],
            run.is_rate_limited,
        )

    def _update_cache(self, repo: str, run: LabelRun) -> None:
        """Write the run's labels and moved issues to the cache now, re-triaged, so views and
        a re-preview don't wait for the next sync. Best effort: a failure here is logged with
        its cause and never fails the apply (the next sync corrects the cache)."""
        try:
            state = self.planner.watched(repo)
            self._store.replace_labels(repo, run.labels)
            for number, labels in run.relabelled.items():
                cached = self._store.issue(repo, number)
                if cached is None:
                    continue  # labelled on GitHub after the last sync: the sync brings it
                result = triage(labels, cached.sub_issue_count, state.schema)
                self._store.update_issue_labels(cached, labels, result)
        except Exception:  # error-isolated: the GitHub writes already happened
            log.exception("issues.labels: updating the cache of %s after an apply failed", repo)
