# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pending-changes tray and approvals (spec §6.1, §6.2; plan Task 6; rulings IR14, IR16,
IR18, IR36).

Two ways reach GitHub, through one engine:

- **Approving a suggestion applies at once** (``ApplyService.approve``): the approval is the
  go-ahead. Its premise is checked first against the cache (an edited draft whose dimension was
  filled since, say, is refused with ``SuggestionStaleError`` and nothing is applied). A
  duplicate needs the original's database id from the cache (``DuplicateNotCachedError``
  otherwise). A complete apply marks the suggestion ``approved``; a failed one leaves it
  approvable, with the cause in ``apply_error``.
- **Every other GitHub-changing action stages** in the tray (``PendingService.stage``: drags,
  pickers, menus, the bulk bar, queue moves) until ``ApplyService.apply_pending`` sends them as
  one batch.

Every batch is stored with its result, so ``retry`` re-runs only the steps that didn't apply
(and an approval's suggestion is marked approved once its batch completes).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from jesinia.apps.issues.application.apply import ApplyEngine
from jesinia.apps.issues.application.labels import Clock
from jesinia.apps.issues.application.ports import (
    AppliedBatch,
    ApplyOrigin,
    ApplyStore,
    CachedIssue,
    RepoState,
)
from jesinia.apps.issues.domain.changes import (
    BatchNotFoundError,
    BatchResult,
    ChangeBatch,
    Existing,
    InvalidChangeError,
    IssueChange,
    NothingPendingError,
    PendingChange,
    PendingNotFoundError,
    SetParent,
    approval_batch,
    check_change,
    parse_source,
    pending_batch,
)
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA
from jesinia.apps.issues.domain.suggestions import (
    OPEN_STATES,
    DuplicateNotCachedError,
    Suggestion,
    SuggestionNotFoundError,
    SuggestionStaleError,
    SuggestionStateError,
    apply_failed,
    approved,
    premise_holds,
)
from jesinia.apps.issues.domain.triage import IssueNotCachedError
from jesinia.services.github.domain.repo import validate_repo

log = logging.getLogger(__name__)


def _new_id() -> str:
    return uuid.uuid4().hex


class PendingCache(Protocol):
    """The issue cache the tray and approvals read (``SqliteIssuesStore`` has it)."""

    def repo(self, repo: str) -> RepoState | None: ...

    def cached_issue(self, repo: str, n: int) -> CachedIssue | None: ...


class ApprovalStore(Protocol):
    """The suggestions an approval reads and settles (``SqliteSuggestionStore`` has it)."""

    def suggestion(self, suggestion_id: str) -> Suggestion | None: ...

    def save(self, suggestion: Suggestion) -> None: ...

    def claim(self, suggestion_id: str, now: datetime) -> bool:
        """Claim an open suggestion for an approval, so retiring and reruns leave it."""

    def release(self, suggestion_id: str) -> None: ...


def _cached(cache: PendingCache, repo: str, n: int) -> CachedIssue:
    found = cache.cached_issue(validate_repo(repo), n)
    if found is None:
        raise IssueNotCachedError(f"{repo}#{n} is not in the cache; sync the repo first")
    return found


def original(cache: PendingCache, repo: str, n: int) -> tuple[Existing, int]:
    """The issue a duplicate points at, and its GitHub database id, from the cache.

    Raises:
        DuplicateNotCachedError: it isn't cached (its id is unknown).
    """
    found = cache.cached_issue(validate_repo(repo), n)
    if found is None:
        raise DuplicateNotCachedError(
            f"{repo}#{n} is not in the cache, so its GitHub id is unknown; sync its repo first"
        )
    return Existing(found.issue.repo, n), found.issue.id


def applied_batch(store: ApplyStore, batch_id: str) -> AppliedBatch:
    """Raises: BatchNotFoundError."""
    found = store.batch(batch_id)
    if found is None:
        raise BatchNotFoundError(f"no applied batch {batch_id}")
    return found


class PendingService:
    """The tray: changes staged by the author's own actions, kept until Apply. Needs no GitHub.

    Args:
        store: the tray (``issues.sqlite``).
        cache: the issue cache.
        clock: the current UTC time.
        new_id: makes pending change ids.
    """

    def __init__(
        self,
        store: ApplyStore,
        cache: PendingCache,
        clock: Clock,
        *,
        new_id: Callable[[], str] = _new_id,
    ) -> None:
        self._store = store
        self._cache = cache
        self._clock = clock
        self._new_id = new_id

    def stage(self, repo: str, n: int, change: IssueChange, source: str) -> PendingChange:
        """Stage ``change`` for the cached issue ``repo#n``; nothing reaches GitHub.

        Raises:
            InvalidRepoError: ``repo`` isn't ``owner/name``.
            InvalidChangeError: an unknown source, a value the repo's schema lacks, or a change
                the issue can't take (its own parent, say).
            IssueNotCachedError: the issue isn't cached.
        """
        where = parse_source(source)
        cached = _cached(self._cache, repo, n)
        target = Existing(cached.issue.repo, n)
        state = self._cache.repo(target.repo)
        check_change(change, DEFAULT_SCHEMA if state is None else state.schema)
        if isinstance(change, SetParent):
            # Staged against the parent the author saw: a move on GitHub since is a conflict.
            seen = cached.issue.parent
            expected = None if seen is None else Existing(seen.repo, seen.number)
            change = replace(change, expects_parent=True, expected_parent=expected)
        ChangeBatch.of({target: [change]})  # the batch rules (no self-parent, same owner)
        staged = PendingChange(self._new_id(), target, change, self._clock(), where)
        self._store.add_pending(staged)
        return staged

    def original(self, repo: str, n: int) -> tuple[Existing, int]:
        """See ``original``."""
        return original(self._cache, repo, n)

    def pending(self) -> list[PendingChange]:
        return self._store.pending()

    def unstage(self, pending_id: str) -> None:
        """Undo one staged change.

        Raises:
            PendingNotFoundError: no such staged change.
        """
        if not self._store.delete_pending([pending_id]):
            raise PendingNotFoundError(f"no pending change {pending_id}")

    def clear(self) -> None:
        self._store.clear_pending()

    def batch(self, batch_id: str) -> AppliedBatch:
        """An applied batch and its result (read from the cache; no GitHub).

        Raises:
            BatchNotFoundError: no such batch.
        """
        return applied_batch(self._store, batch_id)


@dataclass(frozen=True)
class Approval:
    """An approved suggestion as it stands after its apply, and the batch that applied it."""

    suggestion: Suggestion
    batch: AppliedBatch


class ApplyService:
    """Applies the tray and approvals through the engine, and keeps every batch for Retry.
    Batches run one at a time.

    Args:
        engine: the apply engine.
        store: the tray and the applied batches.
        suggestions: the suggestion drafts.
        cache: the issue cache.
        clock: the current UTC time.
        new_id: makes batch ids.
    """

    def __init__(
        self,
        engine: ApplyEngine,
        store: ApplyStore,
        suggestions: ApprovalStore,
        cache: PendingCache,
        clock: Clock,
        *,
        new_id: Callable[[], str] = _new_id,
    ) -> None:
        self._engine = engine
        self._store = store
        self._suggestions = suggestions
        self._cache = cache
        self._clock = clock
        self._new_id = new_id
        self._lock = asyncio.Lock()

    async def apply(self, batch: ChangeBatch, *, origin: ApplyOrigin) -> AppliedBatch:
        """Apply ``batch`` and store it with its result."""
        applied, _ = await self._apply(batch, origin, None)
        return applied

    async def _apply(
        self, batch: ChangeBatch, origin: ApplyOrigin, suggestion: Suggestion | None
    ) -> tuple[AppliedBatch, Suggestion | None]:
        """Apply and store ``batch``; an approval's ``suggestion`` is settled (``_settle``) as
        soon as the result is known, before the engine's cache update retires drafts."""
        batch_id = self._new_id()
        stored: list[AppliedBatch] = []
        settled: list[Suggestion] = []

        def keep(result: BatchResult) -> None:
            now = self._clock()
            sid = None if suggestion is None else suggestion.id
            applied = AppliedBatch(batch_id, origin, sid, batch, result, now, now)
            stored.append(applied)
            try:
                self._store.save_batch(applied)
            except Exception:  # the writes happened: report them; only Retry is lost
                log.exception("issues.apply: storing batch %s failed", batch_id)
            if suggestion is not None:
                settled.append(self._settle(suggestion, result))

        await self._engine.apply(batch, on_result=keep)
        return stored[0], settled[0] if settled else None

    async def apply_pending(self, ids: Sequence[str] | None) -> AppliedBatch:
        """Apply the staged changes (only ``ids`` when given) as one batch; they leave the tray
        once the batch is stored, whatever its result (Retry resumes it).

        Raises:
            NothingPendingError: nothing staged matches.
        """
        async with self._lock:
            staged = self._store.pending(ids)
            if not staged:
                raise NothingPendingError("nothing is pending to apply")
            applied = await self.apply(pending_batch(staged), origin="pending")
            self._store.delete_pending([change.id for change in staged])
            return applied

    async def retry(self, batch_id: str) -> AppliedBatch:
        """Run again only the steps of a stored batch that didn't apply.

        Raises:
            BatchNotFoundError: no such batch.
        """
        async with self._lock:
            stored = applied_batch(self._store, batch_id)
            kept: list[AppliedBatch] = []

            def keep(result: BatchResult) -> None:
                retried = replace(stored, result=result, updated_at=self._clock())
                kept.append(retried)
                try:
                    self._store.save_batch(retried)
                except Exception:  # the writes happened: report them
                    log.exception("issues.apply: storing batch %s failed", batch_id)
                if stored.suggestion_id is not None:
                    found = self._suggestions.suggestion(stored.suggestion_id)
                    if found is not None and found.state in OPEN_STATES:
                        self._settle(found, result)

            await self._engine.retry(stored.batch, stored.result, on_result=keep)
            return kept[0]

    async def approve(self, suggestion_id: str) -> Approval:
        """Approve a suggestion: apply it at once (see the module docstring).

        Raises:
            SuggestionNotFoundError: no such suggestion.
            SuggestionStateError: it was approved or dismissed already.
            IssueNotCachedError: its issue isn't cached.
            SuggestionStaleError: its premise is gone, in the cache or on GitHub now (the
                apply's fresh read); nothing of it was written.
            DuplicateNotCachedError: a duplicate whose original isn't cached.
            InvalidChangeError: a kind that can't be approved yet.
        """
        async with self._lock:
            found = self._suggestions.suggestion(suggestion_id)
            if found is None:
                raise SuggestionNotFoundError(f"no suggestion {suggestion_id}")
            if found.state not in OPEN_STATES:
                raise SuggestionStateError(f"suggestion {found.id} is {found.state} already")
            if found.repo is None or found.n is None:
                raise InvalidChangeError(f"a {found.kind} suggestion can't be approved yet")
            cached = _cached(self._cache, found.repo, found.n)
            issue = cached.issue
            if not premise_holds(
                found,
                cached.triage,
                has_parent=issue.parent is not None,
                is_open=issue.state == "open",
            ):
                raise SuggestionStaleError(
                    f"suggestion {found.id} no longer fits {issue.repo}#{issue.number} as it is "
                    "now; nothing was applied"
                )
            duplicate_id = None
            if found.kind == "duplicate":
                _, duplicate_id = original(
                    self._cache, str(found.payload["repo"]), int(str(found.payload["n"]))
                )
            batch = approval_batch(found, duplicate_id)
            if not self._suggestions.claim(found.id, self._clock()):
                raise SuggestionStateError(f"suggestion {found.id} is being approved already")
            try:
                applied, settled = await self._apply(batch, "suggestion", found)
            finally:
                self._suggestions.release(found.id)
            conflict = next((r for r in applied.result.results if r.is_conflict), None)
            if conflict is not None:
                # The fresh read contradicts the premise (I1): nothing of it was written.
                raise SuggestionStaleError(
                    f"suggestion {found.id} no longer fits: {conflict.cause}"
                )
            return Approval(settled or found, applied)

    def _settle(self, suggestion: Suggestion, result: BatchResult) -> Suggestion:
        """Approved when its batch completed; otherwise still open, with the cause."""
        now = self._clock()
        if result.complete:
            settled = approved(suggestion, now)
        else:
            cause = result.first_failure() or result.stopped or "the apply didn't complete"
            settled = apply_failed(suggestion, cause, now)
        self._suggestions.save(settled)
        return settled
