# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pending-changes tray and approvals (spec §6.1, §6.2; plan Task 6; rulings IR14, IR16,
IR18, IR36, IR45, IR46).

Two ways reach GitHub, through one engine:

- **Approving a suggestion applies at once** (``ApplyService.start_approval``, in the
  background): the approval is the go-ahead. Its premise is checked first against the cache
  (an edited draft whose dimension was filled since, say, is refused with
  ``SuggestionStaleError`` and nothing is applied). A
  duplicate needs the original's database id from the cache (``DuplicateNotCachedError``
  otherwise). A complete apply marks the suggestion ``approved``; a failed one leaves it
  approvable, with the cause in ``apply_error``.
- **Every other GitHub-changing action stages** in the tray (``PendingService.stage``: drags,
  pickers, menus, the bulk bar, queue moves, a published theme's labels) until
  ``ApplyService.start_pending`` sends them as one batch. A published theme also stages its
  label setup step (``stage_label``), which runs before the issues' steps.

Every batch is written ahead and kept with its result, step by step, so ``retry`` re-runs only
the steps that didn't apply (and an approval's suggestion is marked approved once its batch
completes). See ``ApplyService``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, cast

from jesinia.apps.issues.application.apply import ApplyEngine, repo_of
from jesinia.apps.issues.application.epics import ancestors_of, check_parent, facts_of
from jesinia.apps.issues.application.labels import Clock
from jesinia.apps.issues.application.ports import (
    AppliedBatch,
    ApplyOrigin,
    ApplyStore,
    CachedIssue,
    OriginBatch,
    RepoState,
    creation_state,
)
from jesinia.apps.issues.domain.changes import (
    CLOSED_SKIP,
    IN_FLIGHT,
    UNSURE,
    BatchAbandonedError,
    BatchNotFoundError,
    BatchResult,
    BatchRunningError,
    BatchStatus,
    ChangeBatch,
    EarlierBatchError,
    EnsureLabel,
    Existing,
    InvalidChangeError,
    IssueChange,
    New,
    NothingPendingError,
    PendingChange,
    PendingHeldError,
    PendingNotFoundError,
    SetParent,
    approval_batch,
    check_change,
    parse_source,
    pending_batch,
)
from jesinia.apps.issues.domain.epics import (
    Facts,
    GroupEpicExistsError,
    Key,
    issue_key,
    proposal_batch,
    proposal_notes,
    proposal_origin,
)
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA
from jesinia.apps.issues.domain.suggestions import (
    OPEN_STATES,
    DuplicateNotCachedError,
    ItemState,
    Suggestion,
    SuggestionNotFoundError,
    SuggestionStaleError,
    SuggestionStateError,
    apply_failed,
    approved,
    premise_holds,
    priority_settled,
)
from jesinia.apps.issues.domain.triage import IssueNotCachedError, RepoNotWatchedError
from jesinia.services.github.application.service import GitHubRequestError
from jesinia.services.github.domain.repo import validate_repo

log = logging.getLogger(__name__)


def _new_id() -> str:
    return uuid.uuid4().hex


class PendingHolders(Protocol):
    """Who holds a staged change: queue items whose Done staged it (``SqliteQueueStore``)."""

    def items_with_pending(self, pending_id: str) -> Sequence[object]: ...


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


def _outcomes(result: BatchResult) -> dict[tuple[str, int], tuple[ItemState, str | None]]:
    """Each issue's outcome in ``result`` (one step per issue in a priority batch)."""
    states: dict[str, ItemState] = {"done": "applied", "failed": "failed", "skipped": "skipped"}
    return {
        # An issue closed since the draft has nothing left to set: it doesn't hold it open.
        step.step.target.key: (
            "unchanged" if step.cause == CLOSED_SKIP else states[step.state],
            step.cause,
        )
        for step in result.results
        if step.step.target is not None
    }


# The temp ids each kind of origin creates (a pruned batch can be checked only this far).
_TEMP_PREFIX = {"split": "part-", "group": "epic", "theme": "epic", "conversion": "item-"}


def _unknown_create(live: AppliedBatch | None, origin_key: str, temp: str) -> bool:
    """Whether ``temp`` is a create the author may confirm on GitHub (R4, S3): one that was
    attempted and not recorded done, so it may have gone out (cut off while being sent, or its
    answer lost to a timeout or a dropped connection). A create that never ran can't have.
    For a pruned batch, only that the origin's kind creates such a temp id."""
    if live is None:
        prefix = _TEMP_PREFIX.get(origin_key.split(":", 1)[0], "")
        return bool(prefix) and temp.startswith(prefix)
    return any(
        r.step.kind == "create_issue"
        and isinstance(r.step.target, New)
        and r.step.target.temp_id == temp
        and (r.cause in (IN_FLIGHT, UNSURE) or (r.state == "failed" and _attempted(r.cause)))
        for r in live.result.results
    )


def _attempted(cause: str | None) -> bool:
    """A failed step's cause says it was sent (not one the engine never ran)."""
    return cause is not None and not cause.startswith("not run")


def _approval_key(suggestion_id: str, key: str | None) -> str | None:
    return None if key is None else f"{suggestion_id}:{key}"


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
        holders: PendingHolders | None = None,
    ) -> None:
        self._store = store
        self._cache = cache
        self._clock = clock
        self._new_id = new_id
        self._holders = holders

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
            if isinstance(change.parent, Existing):
                # Refused while dragging, with the reason (IR17): nothing is staged.
                check_parent(self._cache, change.parent, target)
            # Staged against the parent the author saw: a move on GitHub since is a conflict.
            seen = cached.issue.parent
            expected = None if seen is None else Existing(seen.repo, seen.number)
            change = replace(change, expects_parent=True, expected_parent=expected)
        ChangeBatch.of({target: [change]})  # the batch rules (no self-parent, same owner)
        staged = PendingChange(self._new_id(), target, change, self._clock(), where)
        self._store.add_pending(staged)
        return staged

    def stage_label(self, change: EnsureLabel, source: str) -> PendingChange:
        """Stage a repo's label setup step (a published theme's ``theme:<tag>``, Task 10): when
        the tray is applied it runs before every issue's steps, through the label executor, and
        is the only way the label is created (IR4). Nothing reaches GitHub until then.

        Raises:
            InvalidChangeError: an unknown source.
            RepoNotWatchedError: the repo isn't watched.
        """
        where = parse_source(source)
        if self._cache.repo(change.repo) is None:
            raise RepoNotWatchedError(f"{change.repo} is not a watched repo; add it first")
        staged = PendingChange(self._new_id(), None, change, self._clock(), where)
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
            PendingHeldError: a queue item's Done holds it (undo that Done instead, so the
                queue never shows Done for a close that will never happen).
            PendingNotFoundError: no such staged change.
        """
        if self._held(pending_id):
            raise PendingHeldError(
                f"pending change {pending_id} is a queue item's Done; undo it from its queue"
            )
        if not self._store.delete_pending([pending_id]):
            raise PendingNotFoundError(f"no pending change {pending_id}")

    def clear(self) -> None:
        """Undo every staged change no queue item's Done holds (those stay: undo them from
        their queue)."""
        loose = [p.id for p in self._store.pending() if not self._held(p.id)]
        self._store.delete_pending(loose)

    def _held(self, pending_id: str) -> bool:
        return self._holders is not None and bool(self._holders.items_with_pending(pending_id))

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
    """Applies the tray and approvals through the engine, and keeps every batch for Retry
    (rulings IR45, IR46).

    - **Write-ahead:** a batch is stored ``running`` before its first GitHub write, with every
      step ``not run yet``, and updated after each step; ``finished`` once its result is final.
    - **Background:** ``start_pending``, ``start_retry`` and ``start_approval`` store the batch and
      return at once; one task per batch runs it, and batches still run one at a time, in the
      order started. Clients poll the batch. ``apply_pending``, ``retry`` and ``approve`` do the
      same and wait for the result.
    - **Replays:** a request's Idempotency-Key is kept on the batch it started; the same key
      always answers that batch, so a replay never applies the tray again.
    - **Stops:** ``stop`` cancels the running batches, which are marked ``interrupted``; ``recover``
      (at start) marks those a crash left ``running``. Retry resumes either without repeating a
      recorded step; a comment or new issue cut off mid-write fails once ("check GitHub")
      rather than being sent blindly again. A replay never re-runs a batch. An approval's claim
      (IR40) is released whatever happens.

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
        self._tasks: dict[str, asyncio.Task[AppliedBatch]] = {}

    # Starting ---------------------------------------------------------------------------

    def _begin(
        self,
        batch: ChangeBatch,
        origin: ApplyOrigin,
        suggestion_id: str | None,
        key: str | None,
        origin_key: str | None = None,
        notes: Mapping[str, str] | None = None,
    ) -> AppliedBatch:
        """Store ``batch`` as ``running``, every step not run yet (write-ahead), with its
        origin's ledger row in the same transaction when it may create issues (N3)."""
        applied = self._new(batch, origin, suggestion_id, key)
        if origin_key is None:
            self._store.save_batch(applied)
        else:
            self._store.begin_batch(applied, origin_key, notes or {})
        return applied

    def _new(
        self,
        batch: ChangeBatch,
        origin: ApplyOrigin,
        suggestion_id: str | None,
        key: str | None,
    ) -> AppliedBatch:
        now = self._clock()
        return AppliedBatch(
            self._new_id(),
            origin,
            suggestion_id,
            batch,
            BatchResult.not_run(batch),
            now,
            now,
            status="running",
            request_key=key,
        )

    def _by_key(self, key: str | None) -> AppliedBatch | None:
        return None if key is None else self._store.batch_by_key(key)

    def _resume_lost(self, applied: AppliedBatch, claimed: str | None) -> None:
        """A replay found its batch ``running`` with no task here (one a restart's recovery
        hasn't seen yet): resume it, never repeating a done step. A finished or interrupted
        batch is left as it is: resuming that is Retry's job, never a replay's."""
        if applied.status == "running" and applied.id not in self._tasks:
            self._spawn(applied, claimed, resume=True)

    def begin_pending(self, ids: Sequence[str] | None, key: str | None) -> AppliedBatch:
        """Take the staged changes (only ``ids`` when given) out of the tray into a stored
        ``running`` batch; the same ``key`` answers the batch it already started.

        Raises:
            NothingPendingError: nothing staged matches.
        """
        if (found := self._by_key(key)) is not None:
            return found
        staged = self._store.pending(ids)
        if not staged:
            raise NothingPendingError("nothing is pending to apply")
        applied = self._new(pending_batch(staged), "pending", None, key)
        self._store.start_batch(applied, [change.id for change in staged])
        return applied

    def start_pending(self, ids: Sequence[str] | None, key: str | None) -> AppliedBatch:
        """``begin_pending``, then run the batch in the background. A replay of ``key``
        answers its batch as it stands and never runs it again (``_resume_lost``)."""
        if (found := self._by_key(key)) is not None:
            self._resume_lost(found, None)
            return found
        applied = self.begin_pending(ids, key)
        self._spawn(applied, None)
        return applied

    async def apply_pending(self, ids: Sequence[str] | None) -> AppliedBatch:
        """``begin_pending`` and wait for the result."""
        return await self._run(self.begin_pending(ids, None), None)

    def start_batch(
        self,
        make: Callable[[], ChangeBatch],
        origin: ApplyOrigin,
        key: str | None,
        origin_key: str | None = None,
        notes: Callable[[], Mapping[str, str]] | None = None,
    ) -> AppliedBatch:
        """Store the batch ``make`` builds (an approved checklist conversion, say) ``running``
        and apply it in the background (IR45). A replay of ``key`` answers its batch as it
        stands, without calling ``make`` and never running it again (IR46). With
        ``origin_key`` (an origin that may create issues: a conversion, a theme's epic) an
        earlier batch for it that is running or unfinished refuses this one, and the batch is
        recorded for it, with ``notes()`` (asked after ``make``), in the same transaction as its
        first save (IR56, N3).

        Raises:
            EarlierBatchError: see ``guard_origin``.
            Whatever ``make`` raises; nothing is stored then.
        """
        if (found := self._by_key(key)) is not None:
            self._resume_lost(found, None)
            return found
        if origin_key is not None:
            self.guard_origin(origin_key)
        batch = make()
        written = None if notes is None else notes()
        applied = self._begin(batch, origin, None, key, origin_key, written)
        self._spawn(applied, None)
        return applied

    def guard_origin(self, origin_key: str) -> list[OriginBatch]:
        """The settled (complete or abandoned) batches recorded for ``origin_key``, oldest
        first, unless an earlier one is running or didn't finish: then approving again could
        create its issues a second time, so it is retried or abandoned instead (rulings IR56,
        IR58). A pruned batch answers from the ledger.

        Raises:
            EarlierBatchError: an earlier batch is running, or finished or was cut off with a
                step not done.
        """
        settled: list[OriginBatch] = []
        for row in self._store.origin_batches(origin_key):
            live = self._store.batch(row.batch_id)
            state = row.state if live is None else creation_state(live)
            if state == "running" or row.batch_id in self._tasks:
                raise EarlierBatchError(
                    row.batch_id,
                    f"an earlier batch for this ({row.batch_id}) is still running; wait for it",
                )
            if state == "incomplete" and live is None:
                raise EarlierBatchError(
                    row.batch_id,
                    f"an earlier batch for this ({row.batch_id}) didn't finish and was pruned, "
                    "and it may have created issues; check GitHub, then abandon batch "
                    f"{row.batch_id} (confirming any issue it created)",
                )
            if state == "incomplete":
                raise EarlierBatchError(
                    row.batch_id,
                    f"an earlier batch for this ({row.batch_id}) didn't finish, and it may have "
                    f"created issues; retry batch {row.batch_id}, or abandon it, instead of "
                    "approving again",
                )
            settled.append(row)
        return settled

    def _settle_done(self, found: Suggestion, done: OriginBatch) -> None:
        """A complete batch already applied this draft's target: the draft whose batch it was
        is settled (its settling was lost); another draft is refused and left open (R3).

        Raises:
            SuggestionStateError: always.
        """
        live = self._store.batch(done.batch_id)
        owner = done.notes.get("suggestion") or (None if live is None else live.suggestion_id)
        if owner == found.id:
            self._suggestions.save(approved(found, self._clock()))
            raise SuggestionStateError(
                f"suggestion {found.id} was applied by batch {done.batch_id} already"
            )
        if found.kind == "split":
            whole = cast(dict[str, object], found.payload["issue"])
            what = f"{whole['repo']}#{whole['n']} was split"
        else:
            what = "those issues were grouped"
        raise SuggestionStateError(
            f"{what} already by batch {done.batch_id}; dismiss suggestion {found.id}"
        )

    @staticmethod
    def _check_reuse(
        found: Suggestion,
        settled: Sequence[OriginBatch],
        reused: Mapping[str, tuple[str, int]],
        notes: Mapping[str, str],
    ) -> None:
        """Reused parts must be the parts the abandoned batch made (R5): the same title at the
        same place, else the approved draft wouldn't be what is applied.

        Raises:
            SuggestionStaleError: an abandoned batch made other parts.
        """
        for row in settled:
            for temp in row.created:
                made = row.notes.get(f"title:{temp}")
                if (
                    temp.startswith("part-")
                    and made is not None
                    and notes.get(f"title:{temp}") != made
                ):
                    named = ", ".join(
                        f"{repo}#{n} {row.notes.get(f'title:{t}', '')}".rstrip()
                        for t, (repo, n) in sorted(row.created.items())
                    )
                    raise SuggestionStaleError(
                        f"abandoned batch {row.batch_id} made other parts than suggestion "
                        f"{found.id} proposes: {named}; approve a draft with the same parts, or "
                        "link those by hand"
                    )

    def _check_overlap(self, found: Suggestion, origin_key: str) -> None:
        """A new group never gets a second epic for issues another group was made for (ruling
        IR60): one still running or unfinished may have made its epic (retry or abandon it), and
        one whose epic exists and isn't known to be closed takes the issues instead. Only an
        abandoned group that made no epic, or one whose epic is closed or gone, is passed by.

        Raises:
            EarlierBatchError: an overlapping group's batch is running or didn't finish.
            GroupEpicExistsError: it overlaps a group whose epic exists and is open.
        """
        mine = set(proposal_notes(found)["members"].split(","))
        for other, row in self._store.origins("group:"):
            shared = mine & set(row.notes.get("members", "").split(",")) - {""}
            if other == origin_key or not shared:
                continue
            names = ", ".join(f"#{ref.rsplit('#', 1)[1]}" for ref in sorted(shared))
            live = self._store.batch(row.batch_id)
            state = row.state if live is None else creation_state(live)
            if state in ("running", "incomplete") or row.batch_id in self._tasks:
                raise EarlierBatchError(
                    row.batch_id,
                    f"retry or abandon batch {row.batch_id} first; it may have made an epic for "
                    f"{names}",
                )
            epic = row.created.get("epic")
            if epic is None:
                continue
            cached = facts_of(self._cache, *epic)
            if cached is not None and not cached.is_open:
                continue
            raise GroupEpicExistsError(
                row.batch_id,
                epic,
                f"an epic for {names} already exists ({epic[0]}#{epic[1]}); add the issues to it",
            )

    async def _epic_gone(self, exc: GroupEpicExistsError) -> bool:
        """Whether a blocking group epic is gone from GitHub (deleted, or transferred to another
        repo), read live; if so the ledger forgets it, so it blocks no more (S4)."""
        repo, n = exc.epic
        try:
            found = await self._engine.github.get(repo, n)
        except GitHubRequestError as error:
            if error.status not in (404, 410):
                raise
            gone = True
        else:
            gone = repo_of(found).casefold() != repo.casefold()
        if gone:
            self._store.forget_epic(exc.batch_id)
        return gone

    async def _begin_live(self, suggestion_id: str, key: str | None) -> Approval:
        """``begin_approval``, after a live read of a group epic that blocks it, which the cache
        can't tell is gone (GitHub's deletions never reach an incremental sync)."""
        while True:
            try:
                return self.begin_approval(suggestion_id, key)
            except GroupEpicExistsError as exc:
                if not await self._epic_gone(exc):
                    raise

    async def start_approval_live(self, suggestion_id: str, key: str | None) -> Approval:
        """``start_approval``, with a blocking group epic read live first (see
        ``_begin_live``)."""
        if self._approval_by_key(suggestion_id, key) is not None:
            return self.start_approval(suggestion_id, key)  # a replay answers its approval
        approval = await self._begin_live(suggestion_id, key)
        self._spawn(approval.batch, approval.suggestion.id)
        return approval

    @staticmethod
    def reused(settled: Sequence[OriginBatch]) -> dict[str, tuple[str, int]]:
        """The issues earlier settled batches created, by temp id (a later one wins)."""
        found: dict[str, tuple[str, int]] = {}
        for row in settled:
            found.update(row.created)
        return found

    def abandon(
        self, batch_id: str, confirmed: Mapping[str, tuple[str, int]]
    ) -> tuple[str, OriginBatch]:
        """Give up on an unfinished batch of an origin that creates issues (ruling IR58), even
        one already pruned: it never runs again, and the issues it created (its done creates,
        plus those ``confirmed`` on GitHub for a create whose result is unknown) are recorded,
        so the next approval for its origin reuses them and creates only what is missing.
        Abandoning it again changes nothing. Answers its origin and its ledger row.

        Raises:
            BatchNotFoundError: no batch recorded for an origin that creates issues.
            BatchRunningError: it is still running.
            InvalidChangeError: it completed (nothing to abandon), or a confirmed temp id isn't
                one of its creates still unknown.
        """
        found = self._store.origin_of(batch_id)
        if found is None:
            raise BatchNotFoundError(f"no batch {batch_id} of an origin that creates issues")
        origin_key, row = found
        live = self._store.batch(batch_id)
        state = row.state if live is None else creation_state(live)
        if state == "running" or batch_id in self._tasks:
            raise BatchRunningError(f"batch {batch_id} is still running; poll it")
        if state == "abandoned":
            return origin_key, row
        if state == "complete":
            raise InvalidChangeError(f"batch {batch_id} completed; there is nothing to abandon")
        for temp in confirmed:
            if temp in row.created or not _unknown_create(live, origin_key, temp):
                raise InvalidChangeError(
                    f"{temp!r} isn't a create of batch {batch_id} whose result is unknown"
                )
        created = {**row.created, **confirmed}
        self._store.abandon(batch_id, created, self._clock())
        return origin_key, replace(row, state="abandoned", created=created)

    async def apply(self, batch: ChangeBatch, *, origin: ApplyOrigin) -> AppliedBatch:
        """Store ``batch``, apply it and wait for the result."""
        return await self._run(self._begin(batch, origin, None, None), None)

    def begin_retry(self, batch_id: str) -> AppliedBatch:
        """Mark a stored batch ``running`` again, to resume what didn't apply.

        Raises:
            BatchNotFoundError: no such batch.
            BatchRunningError: it is still running.
        """
        stored = applied_batch(self._store, batch_id)
        if stored.status == "running" or batch_id in self._tasks:
            # A task still holds it (its result may be stored while the cache update runs).
            raise BatchRunningError(f"batch {batch_id} is still running; poll it")
        if stored.status == "abandoned":
            raise BatchAbandonedError(
                f"batch {batch_id} was abandoned; approve it anew, which reuses what it created"
            )
        again = replace(stored, status="running", updated_at=self._clock())
        self._store.save_batch(again)
        return again

    def start_retry(self, batch_id: str) -> AppliedBatch:
        """``begin_retry``, then run it in the background."""
        again = self.begin_retry(batch_id)
        self._spawn(again, self._retry_claim(again), resume=True)
        return again

    async def retry(self, batch_id: str) -> AppliedBatch:
        """``begin_retry`` and wait for the result."""
        again = self.begin_retry(batch_id)
        return await self._run(again, self._retry_claim(again), resume=True)

    def _retry_claim(self, applied: AppliedBatch) -> str | None:
        """A resumed approval protects its open draft like the approval did (IR40)."""
        sid = applied.suggestion_id
        return sid if sid is not None and self._suggestions.claim(sid, self._clock()) else None

    def begin_approval(self, suggestion_id: str, key: str | None) -> Approval:
        """Check an approval, claim its draft and store its batch ``running``, keeping ``key``
        (``start_approval`` answers a replay of it before calling this).

        Raises:
            SuggestionNotFoundError: no such suggestion.
            SuggestionStateError: it was approved or dismissed already, or is being approved.
            IssueNotCachedError: its issue isn't cached.
            SuggestionStaleError: its premise is gone in the cache; nothing is applied.
            DuplicateNotCachedError: a duplicate whose original isn't cached.
            InvalidChangeError: a kind that can't be approved yet.
        """
        found = self._suggestions.suggestion(suggestion_id)
        if found is None:
            raise SuggestionNotFoundError(f"no suggestion {suggestion_id}")
        if found.state not in OPEN_STATES:
            raise SuggestionStateError(f"suggestion {found.id} is {found.state} already")
        origin_key: str | None = None
        reused: dict[str, tuple[str, int]] = {}
        if found.kind in ("group", "split"):
            # Its target's ledger, whatever draft proposed it (IR56, IR57): retried or
            # abandoned, never approved again over an unfinished batch.
            origin_key = proposal_origin(found)
        notes: dict[str, str] = {}
        if origin_key is not None:
            settled = self.guard_origin(origin_key)
            done = [row for row in settled if row.state == "complete"]
            if done:
                self._settle_done(found, done[-1])
            reused = self.reused(settled)
            notes = proposal_notes(found)
            self._check_reuse(found, settled, reused, notes)
            if found.kind == "group" and "epic" not in reused:
                self._check_overlap(found, origin_key)
        if found.kind == "priority":
            # About a batch of issues: each change is guarded by the label the draft saw.
            batch = approval_batch(found)
        elif found.kind in ("group", "split"):
            batch = self._proposal_batch(found, reused)
        else:
            batch = self._issue_batch(found)
        if not self._suggestions.claim(found.id, self._clock()):
            raise SuggestionStateError(f"suggestion {found.id} is being approved already")
        try:
            applied = self._begin(
                batch, "suggestion", found.id, _approval_key(found.id, key), origin_key, notes
            )
        except BaseException:
            self._release(found.id)
            raise
        return Approval(found, applied)

    def _approval_by_key(self, suggestion_id: str, key: str | None) -> Approval | None:
        """The approval this ``key`` already started for this suggestion (a key is scoped to
        its suggestion: one key on two approvals is two approvals)."""
        started = self._by_key(_approval_key(suggestion_id, key))
        if started is None:
            return None
        current = self._suggestions.suggestion(suggestion_id)
        if current is None:
            raise SuggestionNotFoundError(f"no suggestion {suggestion_id}")
        return Approval(current, started)

    def start_approval(self, suggestion_id: str, key: str | None) -> Approval:
        """``begin_approval``, then apply it in the background (the request never waits). A
        replay of ``key`` answers its approval as it stands and never runs it again."""
        if (replayed := self._approval_by_key(suggestion_id, key)) is not None:
            batch = replayed.batch
            if batch.status == "running" and batch.id not in self._tasks:
                self._resume_lost(batch, self._retry_claim(batch))
            return replayed
        approval = self.begin_approval(suggestion_id, key)
        self._spawn(approval.batch, approval.suggestion.id)
        return approval

    async def approve(self, suggestion_id: str) -> Approval:
        """``begin_approval`` and wait for the result.

        Raises:
            SuggestionStaleError: as ``begin_approval``, or when the apply's fresh read
                contradicts the premise (nothing of it was written).
            Others: as ``begin_approval``.
        """
        approval = await self._begin_live(suggestion_id, None)
        found = approval.suggestion
        applied = await self._run(approval.batch, found.id)
        settled = self._suggestions.suggestion(found.id) or found
        conflict = next((r for r in applied.result.results if r.is_conflict), None)
        if conflict is not None and found.kind != "priority":
            # The fresh read contradicts the premise (I1): nothing of it was written. (A
            # priority draft covers several issues: those that still fit applied, and the
            # approval's failure names the rest.)
            raise SuggestionStaleError(f"suggestion {found.id} no longer fits: {conflict.cause}")
        return Approval(settled, applied)

    # Running ----------------------------------------------------------------------------

    def _spawn(self, applied: AppliedBatch, claimed: str | None, *, resume: bool = False) -> None:
        task = asyncio.create_task(self._run(applied, claimed, resume=resume))
        self._tasks[applied.id] = task
        task.add_done_callback(lambda done: self._finished(applied.id, claimed, done))

    def _finished(
        self, batch_id: str, claimed: str | None, task: asyncio.Task[AppliedBatch]
    ) -> None:
        """A background run ended: whatever happened, its claim is released, and a run that was
        cancelled (a stop) or broke is ``interrupted``. Best effort: start recovers the rest."""
        # One task per batch at a time: a Retry or a replay never starts another while this one
        # is held (``begin_retry``, ``_resume_lost``), so this is its own entry.
        self._tasks.pop(batch_id, None)
        if claimed is not None:
            self._release(claimed)
        if not task.cancelled() and task.exception() is None:
            return
        if not task.cancelled():
            log.error("issues.apply: batch %s failed", batch_id, exc_info=task.exception())
        try:
            stored = self._store.batch(batch_id)
            if stored is not None and stored.status == "running":
                self._store.save_batch(replace(stored, status="interrupted"))
        except Exception:
            log.exception("issues.apply: marking batch %s interrupted failed", batch_id)

    async def _run(
        self, applied: AppliedBatch, claimed: str | None, *, resume: bool = False
    ) -> AppliedBatch:
        """Run a stored ``running`` batch under the ordering lock, recording each step and the
        final result; an approval's suggestion is settled as soon as the result is known."""
        latest = [applied]

        def record(result: BatchResult, status: BatchStatus) -> None:
            latest[0] = replace(latest[0], result=result, status=status, updated_at=self._clock())
            try:
                self._store.save_batch(latest[0])
            except Exception:  # the writes happened: report them; only the record lags
                log.exception("issues.apply: storing batch %s failed", applied.id)

        def keep(result: BatchResult) -> None:
            record(result, "finished")
            if applied.suggestion_id is not None:
                found = self._suggestions.suggestion(applied.suggestion_id)
                if found is not None and found.state in OPEN_STATES:
                    self._settle(found, result)

        def step(result: BatchResult) -> None:
            record(result, "running")

        try:
            async with self._lock:
                if resume:
                    await self._engine.retry(
                        applied.batch, applied.result, on_result=keep, on_step=step
                    )
                else:
                    await self._engine.apply(applied.batch, on_result=keep, on_step=step)
        except asyncio.CancelledError:
            record(latest[0].result, "interrupted")
            raise
        finally:
            if claimed is not None:
                self._release(claimed)
        return latest[0]

    def recover(self) -> int:
        """At start: mark the batches a stop or crash left ``running`` as ``interrupted``."""
        return self._store.interrupt_running()

    async def drain(self) -> None:
        """Wait for the background batches started so far."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def stop(self) -> None:
        """Cancel the background batches (each is marked ``interrupted``) and wait for them."""
        for task in list(self._tasks.values()):
            task.cancel()
        await self.drain()

    def _issue_batch(self, found: Suggestion) -> ChangeBatch:
        """The batch approving a suggestion about one issue makes, after its premise check.

        Raises:
            IssueNotCachedError, SuggestionStaleError, DuplicateNotCachedError,
            InvalidChangeError: see ``approve``.
        """
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
        return approval_batch(found, duplicate_id)

    def _proposal_batch(
        self, found: Suggestion, reused: Mapping[str, tuple[str, int]]
    ) -> ChangeBatch:
        """The batch approving a ``group`` or ``split`` draft makes, after its premise check
        against the cache (``domain.epics.proposal_batch``).

        Raises:
            SuggestionStaleError: see ``proposal_batch``.
        """
        payload = found.payload
        refs = [
            cast(dict[str, object], payload[name]) for name in ("epic", "issue") if name in payload
        ]
        refs += cast(list[dict[str, object]], payload.get("issues", []))
        if "epic" in reused:  # the epic an abandoned batch made (IR58)
            refs.append({"repo": reused["epic"][0], "n": reused["epic"][1]})
        known: dict[Key, Facts] = {}
        ancestors: dict[Key, tuple[Key, ...]] = {}
        for ref in refs:
            repo, n = str(ref["repo"]), cast(int, ref["n"])
            facts = facts_of(self._cache, repo, n)
            if facts is not None:
                known[facts.key] = facts
                ancestors[issue_key(repo, n)] = ancestors_of(self._cache, repo, n)
        return proposal_batch(found, known, ancestors, reused)

    def _release(self, suggestion_id: str) -> None:
        """Release a claim, best effort: a failure here must not hide the apply's own outcome
        or cause (start releases what is left)."""
        try:
            self._suggestions.release(suggestion_id)
        except Exception:
            log.exception("issues.apply: releasing the claim on suggestion %s", suggestion_id)

    def _settle(self, suggestion: Suggestion, result: BatchResult) -> Suggestion:
        """Approved when its batch completed; otherwise still open, with the cause."""
        now = self._clock()
        if suggestion.kind == "priority":
            settled = priority_settled(suggestion, _outcomes(result), now)
            self._suggestions.save(settled)
            return settled
        if result.complete:
            settled = approved(suggestion, now)
        else:
            cause = result.first_failure() or result.stopped or "the apply didn't complete"
            settled = apply_failed(suggestion, cause, now)
        self._suggestions.save(settled)
        return settled
