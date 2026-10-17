# SPDX-License-Identifier: AGPL-3.0-or-later
"""The label setup and migration batches (spec §2, §6.2): planned, previewed, then applied with
the author's approval through ``LabelExecutor``.

``LabelExecutor`` is the minimal ordered executor for ``LabelOp`` (ruling IR12); Task 6's apply
engine reuses it for its ``EnsureLabel`` steps. It:

- reads the repo's labels fresh before anything else (IR4), so a label is created only by a
  ``CreateLabel`` step and a relabel never asks GitHub to add a label the repo lacks (GitHub
  would create it silently);
- relabels from a fresh read of each issue, so a label someone removed meanwhile isn't touched
  and GitHub's current spelling is the one removed;
- keeps going past a failed step (each issue's result is its own), but runs a ``DeleteLabel``
  only when every earlier step succeeded: if any relabel failed, nothing is deleted and the
  result says so;
- stops the whole batch at a rate limit (IR5), marking the rest skipped and returning GitHub's
  reset time, so the route can answer with ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    Relabel,
    RepoNotSyncedError,
    plan_migration,
    plan_setup,
)
from jesinia.apps.issues.domain.triage import RepoNotWatchedError
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubRequestError,
)
from jesinia.services.github.domain.issue import IssueDomainError
from jesinia.services.github.domain.repo import validate_repo

StepState = Literal["done", "failed", "skipped"]
Clock = Callable[[], datetime]
# Told GitHub's reset time when a batch hit the rate limit (the sync pauses until then).
OnRateLimit = Callable[[datetime | None], None]


@dataclass(frozen=True)
class OpResult:
    """One op's outcome; ``cause`` says why it failed or was skipped (or a no-op's reason)."""

    op: LabelOp
    state: StepState
    cause: str | None = None


@dataclass(frozen=True)
class LabelRun:
    """A batch's outcome, op by op, in order.

    ``is_rate_limited`` means GitHub stopped the batch (``rate_limited_until`` is its reset
    time, when it gave one); ``note`` says why old labels were kept, if they were.
    """

    results: list[OpResult]
    is_rate_limited: bool = False
    rate_limited_until: datetime | None = None
    note: str | None = None

    @property
    def complete(self) -> bool:
        return all(result.state == "done" for result in self.results)


class _StepFailedError(Exception):
    """An op that can't run, with the cause to report (internal to the executor)."""


def _key(label: str) -> str:
    return label.strip().casefold()


class LabelExecutor:
    """Runs ``LabelOp`` in order against one repo (see the module docstring)."""

    def __init__(self, github: LabelWrites) -> None:
        self._github = github

    async def run(self, repo: str, ops: Sequence[LabelOp]) -> LabelRun:
        """Run ``ops`` on ``repo``.

        Raises:
            GitHubError: the first, fresh read of the repo's labels failed (nothing ran).
        """
        known = {_key(label.name): label.name for label in await self._github.labels(repo)}
        results: list[OpResult] = []
        failed = 0
        note: str | None = None
        for index, op in enumerate(ops):
            if isinstance(op, DeleteLabel) and failed:
                note = (
                    f"Nothing was deleted: {failed} step{'s' if failed > 1 else ''} failed, "
                    "so the old labels were kept."
                )
                results.append(OpResult(op, "skipped", note))
                continue
            try:
                cause = await self._run(repo, op, known)
            except GitHubRateLimitedError as exc:
                stopped = f"not run: the batch stopped at GitHub's rate limit ({exc})"
                results.append(OpResult(op, "failed", str(exc)))
                results.extend(OpResult(rest, "skipped", stopped) for rest in ops[index + 1 :])
                return LabelRun(results, True, exc.reset_at, note)
            except (_StepFailedError, GitHubError, IssueDomainError) as exc:
                failed += 1
                results.append(OpResult(op, "failed", str(exc)))
            else:
                results.append(OpResult(op, "done", cause))
        return LabelRun(results, note=note)

    async def _run(self, repo: str, op: LabelOp, known: dict[str, str]) -> str | None:
        if isinstance(op, CreateLabel):
            if _key(op.name) in known:
                return "already on GitHub"
            created = await self._github.create_label(repo, op.name, op.color, op.description)
            known[_key(created.name)] = created.name
            return None
        if isinstance(op, Relabel):
            return await self._relabel(repo, op, known)
        try:
            await self._github.delete_label(repo, op.name)
        except GitHubRequestError as exc:
            if exc.status != 404:
                raise
            return "already gone"
        return None

    async def _relabel(self, repo: str, op: Relabel, known: dict[str, str]) -> str | None:
        fresh = {_key(label): label for label in (await self._github.get(repo, op.number)).labels}
        pairs = [(fresh[_key(old)], new) for old, new in op.replace if _key(old) in fresh]
        if not pairs:
            return "nothing to relabel: the old labels are gone"
        missing = [new for _, new in pairs if _key(new) not in known]
        if missing:
            raise _StepFailedError(
                f"{', '.join(repr(m) for m in missing)} doesn't exist on GitHub, and Jesinia "
                "never creates labels implicitly; run the label setup first"
            )
        adds = list(dict.fromkeys(known[_key(new)] for _, new in pairs if _key(new) not in fresh))
        if adds:
            await self._github.add_labels(repo, op.number, adds)
        for old in dict.fromkeys(old for old, _ in pairs):
            await self._github.remove_label(repo, op.number, old)
        return None


class LabelPlanner:
    """Plans a repo's label setup or migration from a fresh read of its labels, its stored
    schema and (for a migration) every cached issue."""

    def __init__(self, github: LabelWrites, store: LabelPlanStore) -> None:
        self._github = github
        self._store = store

    def _watched(self, repo: str) -> RepoState:
        state = self._store.repo(validate_repo(repo))
        if state is None or not state.watched:
            raise RepoNotWatchedError(f"{repo} is not a watched repo; add it first")
        return state

    async def _labels(self, repo: str) -> list[str]:
        return [label.name for label in await self._github.labels(repo)]

    async def setup(self, repo: str) -> list[CreateLabel]:
        """The canonical labels the repo is missing.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author hasn't added ``repo``.
            GitHubError: GitHub couldn't be read.
        """
        state = self._watched(repo)
        return plan_setup(state.schema, await self._labels(state.repo))

    async def migrate(self, repo: str) -> MigrationPlan:
        """Every alias on GitHub mapped to its canonical label.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            RepoNotWatchedError: the author hasn't added ``repo``.
            RepoNotSyncedError: the repo never finished a sync, so the cache can't say which
                issues carry an alias.
            GitHubError: GitHub couldn't be read.
        """
        state = self._watched(repo)
        if state.synced_at is None:
            raise RepoNotSyncedError(f"{repo} hasn't finished a sync yet; sync it, then migrate")
        existing = await self._labels(state.repo)
        return plan_migration(state.repo, state.schema, existing, self._store.issues(state.repo))

    async def ops(self, repo: str, kind: LabelPlanKind) -> tuple[LabelOp, ...]:
        if kind == "setup":
            return tuple(await self.setup(repo))
        return tuple((await self.migrate(repo)).ops())


class LabelSetupService:
    """Previews and applies the label batches. Applying is an approval: the route allows the
    owner only and keys the request.

    Args:
        github: label reads and writes (``IssueClient``).
        store: ``issues.sqlite``.
        clock: the current UTC time.
        on_rate_limit: told GitHub's reset time when a batch hit the rate limit.
    """

    def __init__(
        self,
        github: LabelWrites,
        store: LabelPlanStore,
        clock: Clock,
        *,
        on_rate_limit: OnRateLimit | None = None,
    ) -> None:
        self.planner = LabelPlanner(github, store)
        self._executor = LabelExecutor(github)
        self._store = store
        self._clock = clock
        self._on_rate_limit = on_rate_limit
        self._locks: dict[str, asyncio.Lock] = {}

    async def preview(self, repo: str, kind: LabelPlanKind) -> LabelPlan:
        """Plan the batch and keep it under a new id; nothing is written to GitHub.

        Raises:
            InvalidRepoError, RepoNotWatchedError, RepoNotSyncedError, GitHubError: as the
            planner's.
        """
        name = validate_repo(repo)
        ops = await self.planner.ops(name, kind)
        now = self._clock()
        plan = LabelPlan(uuid.uuid4().hex, name, kind, ops, now)
        self._store.save_plan(plan, now)
        return plan

    async def apply(self, repo: str, kind: LabelPlanKind, plan_id: str) -> LabelRun:
        """Apply a previewed plan, once: the plan is consumed whatever happens, and anything
        left to do needs a fresh preview (which plans only what remains). A complete migration
        marks the repo migrated.

        Raises:
            InvalidRepoError: ``repo`` is not ``owner/name``.
            LabelPlanNotFoundError: no such plan for ``repo``.
            LabelPlanKindMismatchError, LabelPlanExpiredError, LabelPlanOutdatedError: the plan
                no longer stands (see ``LabelPlan.check``).
            RepoNotWatchedError, RepoNotSyncedError, GitHubError: as the planner's.
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
            plan.check(kind, self._clock(), await self.planner.ops(name, kind))
            run = await self._executor.run(name, plan.ops)
            if run.is_rate_limited and self._on_rate_limit is not None:
                self._on_rate_limit(run.rate_limited_until)
            if kind == "migrate" and run.complete:
                self._store.set_migrated(name)
            return run
