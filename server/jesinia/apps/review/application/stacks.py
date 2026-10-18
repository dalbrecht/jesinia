# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reviewing a stack of PRs as one unit (spec §4.3), and **Post all**.

A stack review is one Overview, one Background and one stack walkthrough for the whole
stack (``STACK_JOBS``), run through the review agent framework (schema-validated, the PRs'
text fenced as untrusted). Each PR keeps its own round, walkthrough, findings, drafts and
recommendation: opening the stack opens every PR through the pipeline, under its own gates,
and the stack's jobs start only once no PR is held by one.

**Post all** posts each PR's confirmed review bottom-up, one after another, through the
normal posting path (markers, reconcile, round binding R67), and stops at the first PR that
can't be posted. Every PR's attempt is keyed from the request's idempotency key
(``attempt_key``), so a replay of the request adopts or skips what an earlier run posted and
never posts a PR twice. A frozen or stale round anywhere refuses its PR before anything is
posted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from jesinia.apps.review.application.errors import JobFailed
from jesinia.apps.review.application.pipeline import (
    PROGRESS_TOPIC,
    KnowledgeSource,
    NothingKnown,
    OpenResult,
)
from jesinia.apps.review.application.posting import PostedState
from jesinia.apps.review.application.reviews import FINDING_SOURCES, PrView
from jesinia.apps.review.domain.coverage import (
    DiffParseError,
    HunkIndex,
    ParsedDiff,
    hunk_index,
    parse_unified_diff,
)
from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.jobs import STACK_JOBS, JobKind, Round, StackRound
from jesinia.apps.review.domain.posting import (
    NoReviewRound,
    Posting,
    RoundChanged,
    RoundFrozen,
)
from jesinia.apps.review.domain.queue import PrRef, RoundRecord, StoredPr, author_kind
from jesinia.apps.review.domain.stacks import (
    CrossFinding,
    NotAStackError,
    PostAllItem,
    PostAllResult,
    PostAllStopped,
    PostOutcome,
    RoundNotOfPr,
    StackPreparationError,
    agent_cross_findings,
    attempt_key,
    detect_stacks,
    known_stack_walkthrough,
    order_post_all,
    restack_reason,
    shared_file_findings,
    stack_signature,
)
from jesinia.apps.review.domain.workspace import DiffTooLargeError, WorkspaceError
from jesinia.core.events import Event, EventBus
from jesinia.services.github.application.service import GitHubError
from jesinia.services.github.domain.pr import (
    PrCommit,
    PullRequest,
    PullRequestDomainError,
    ReviewEvent,
)

__all__ = ["StackOpenResult", "StackReviews", "StackView"]

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
StackOpenState = Literal["started", "running", "up_to_date", "held"]
# What a PR's open may answer for the stack's jobs to go ahead: anything else is a gate (or
# a failed round) holding that PR, and so the stack.
_GOING: Final = frozenset({"started", "running", "up_to_date"})
# Failures a Post all run stops at (the post's typed errors and GitHub's), reported per PR.
_POST_ERRORS = (ReviewError, GitHubError, PullRequestDomainError)
# A stack job's states that run again only when the author asks (Retry, R60, R73).
_RERUN_ON_RETRY: Final = frozenset({"failed", "interrupted"})
INTERRUPTED = "Jesinia stopped during this job; Retry runs it again"


class StackStore(Protocol):
    """The ``review.sqlite`` reads and writes a stack review needs."""

    def prs(self, include_dismissed: bool = False) -> list[StoredPr]: ...

    def viewer(self) -> str | None: ...

    def round(self, round_id: int) -> RoundRecord | None: ...

    def latest_round(self, ref: PrRef) -> RoundRecord | None: ...

    def postings(self, round_id: int) -> list[Posting]: ...

    def stack_review(self, repo: str, signature: str) -> int | None: ...

    def start_stack_review(self, repo: str, signature: str, started_at: datetime) -> int: ...

    def stack_artifacts(self, stack_id: int) -> dict[str, object]: ...

    def stack_artifact(self, stack_id: int, kind: str) -> object: ...

    def put_stack_artifact(self, stack_id: int, kind: str, data: object) -> None: ...

    def stack_jobs(self) -> dict[int, object]: ...

    def stack_check(self, repo: str, signature: str) -> str | None: ...

    def put_stack_check(self, repo: str, signature: str, reason: str) -> None: ...

    def restacks(self) -> dict[tuple[str, str], str]: ...


class StackOpener(Protocol):
    """Opening one PR (``Pipeline.open``), under its own gates."""

    async def open(self, ref: PrRef) -> OpenResult: ...


class StackPoster(Protocol):
    """Posting one round's review (``PostingService``): the only GitHub write."""

    async def post_unless_posted(
        self,
        round_id: int,
        event: ReviewEvent,
        confirm: Collection[str],
        *,
        key: str,
        digest: str,
    ) -> tuple[str, PostedState]: ...


class StackReads(Protocol):
    """A PR's cached view (``ReviewReads.pr``)."""

    def pr(self, ref: PrRef) -> PrView: ...


class StackJobs(Protocol):
    """The stack's agent jobs (``JobRunner.run_stack``)."""

    async def run_stack(self, job: JobKind, stack: StackRound) -> dict[str, object]: ...


class StackGitHub(Protocol):
    async def get(self, repo: str, number: int) -> PullRequest: ...

    async def commits_of(self, pr: PullRequest) -> tuple[PullRequest, list[PrCommit]]: ...


class StackWorkspace(Protocol):
    async def ensure_mirror(self, repo: str) -> None: ...

    async def fetch(self, repo: str, refs: list[str]) -> None: ...

    async def merge_base(self, repo: str, a: str, b: str) -> str: ...

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str: ...

    async def is_ancestor(self, repo: str, ancestor: str, descendant: str) -> bool: ...


class StackSlots(Protocol):
    """The scheduler's round slots, which the stack's jobs share (R52)."""

    async def run_open[T](self, work: Callable[[], Awaitable[T]]) -> T: ...


class _Unlimited:
    async def run_open[T](self, work: Callable[[], Awaitable[T]]) -> T:
        return await work()


@dataclass(frozen=True)
class StackView:
    """A stack as cached: its PRs bottom-up (each PR's own view), the stack review for their
    current heads (``None`` before one) and its artifacts, and the cross-PR findings."""

    repo: str
    signature: str
    members: tuple[PrView, ...]
    stack_id: int | None
    artifacts: Mapping[str, object]
    cross_findings: tuple[CrossFinding, ...]


@dataclass(frozen=True)
class StackOpenResult:
    """What opening the stack did: each PR's own open (bottom-up), and whether the stack's
    jobs ``started`` (listed), are ``running``, are ``up_to_date``, or are ``held`` by a PR's
    gate (``reason`` names it)."""

    state: StackOpenState
    reason: str
    stack_id: int
    jobs: tuple[JobKind, ...]
    members: tuple[tuple[int, OpenResult], ...]


def _failed(jobs: object) -> set[str]:
    """The kinds a stack's ``jobs`` record marks failed or interrupted."""
    entries = jobs.items() if isinstance(jobs, Mapping) else ()
    return {
        str(kind)
        for kind, entry in entries
        if isinstance(entry, Mapping) and entry.get("state") in _RERUN_ON_RETRY
    }


def _findings(view: PrView) -> list[object]:
    out: list[object] = []
    for source in FINDING_SOURCES:
        data = view.artifacts.get(source)
        items = data.get("findings") if isinstance(data, Mapping) else None
        out += items if isinstance(items, list) else []
    return out


class StackReviews:
    """Stack reviews and **Post all**.

    Args:
        store: ``review.sqlite``.
        reads: each PR's cached view (``ReviewReads``).
        opener: opens each PR (``Pipeline``).
        poster: posts each PR's review (``PostingService``).
        jobs: the stack's agent jobs (``JobRunner``).
        github: each PR and its commits, read for the stack's jobs (``PrClient``).
        workspace: the local mirror, for each PR's diff (``GitWorkspace``).
        clock: the current UTC time.
        slots: the scheduler, whose round slots the stack's jobs wait for.
        bus: where a ``review.progress`` event goes when the stack's jobs end.
        knowledge: the knowledge model, for the stack's knowledge summary.
    """

    def __init__(
        self,
        *,
        store: StackStore,
        reads: StackReads,
        opener: StackOpener,
        poster: StackPoster,
        jobs: StackJobs,
        github: StackGitHub,
        workspace: StackWorkspace,
        clock: Clock,
        bus: EventBus,
        slots: StackSlots | None = None,
        knowledge: KnowledgeSource | None = None,
    ) -> None:
        self._store = store
        self._reads = reads
        self._opener = opener
        self._poster = poster
        self._jobs = jobs
        self._github = github
        self._workspace = workspace
        self._clock = clock
        self._slots: StackSlots = _Unlimited() if slots is None else slots
        self._bus = bus
        self._knowledge: KnowledgeSource = knowledge or NothingKnown()
        self._running: dict[int, set[JobKind]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._verify_task: asyncio.Task[int] | None = None

    # --- the stack ---------------------------------------------------------------------------

    def members(self, ref: PrRef) -> list[StoredPr]:
        """``ref``'s stack, bottom-up, among the cached (open) PRs.

        Raises:
            NotAStackError: the PR isn't cached, is alone, or is in a group the stack rule
                refuses (the reason is in the message).
        """
        stored = self._store.prs(include_dismissed=True)
        if not any(s.pr.ref == ref for s in stored):
            raise NotAStackError(f"{ref} is not in the review queue; load it first")
        detection = detect_stacks([s.pr for s in stored], self._store.restacks())
        found = detection.stack_of(ref.repo, ref.number)
        if found is None:
            why = detection.refusal_of(ref.repo, ref.number)
            raise NotAStackError(f"{ref} isn't part of a stack" + (f": {why}" if why else ""))
        by_ref = {s.pr.ref: s for s in stored}
        return [by_ref[member.ref] for member in found]

    def view(self, ref: PrRef) -> StackView:
        """The stack holding ``ref``, from the cache only.

        Raises:
            NotAStackError: as ``members``.
        """
        members = self.members(ref)
        repo = members[0].pr.ref.repo
        signature = stack_signature([(m.pr.ref.number, m.pr.head_sha) for m in members])
        views = tuple(self._reads.pr(m.pr.ref) for m in members)
        stack_id = self._store.stack_review(repo, signature)
        artifacts = {} if stack_id is None else self._store.stack_artifacts(stack_id)
        shared = shared_file_findings(
            {v.row.number: _findings(v) for v in views},
            {v.row.number: {f.filename for f in v.files or ()} for v in views},
        )
        across = agent_cross_findings(artifacts.get(JobKind.STACK_WALKTHROUGH.value))
        return StackView(repo, signature, views, stack_id, artifacts, (*shared, *across))

    # --- the heads (R77) ---------------------------------------------------------------------

    async def _contained(self, members: Sequence[StoredPr]) -> str:
        """'' when the top head contains every member head, else why each PR is shown
        alone; decided once per set of heads and kept (a restack is a new set).

        Raises:
            WorkspaceError: git couldn't check (nothing is recorded: checked again later).
        """
        repo = members[0].pr.ref.repo
        signature = stack_signature([(m.pr.ref.number, m.pr.head_sha) for m in members])
        known = self._store.stack_check(repo, signature)
        if known is not None:
            return known
        top = members[-1].pr
        await self._workspace.ensure_mirror(repo)
        await self._workspace.fetch(repo, sorted({m.pr.head_sha for m in members}))
        reason = ""
        for member in members[:-1]:
            if not await self._workspace.is_ancestor(repo, member.pr.head_sha, top.head_sha):
                reason = restack_reason(member.pr.ref.number, top.ref.number)
                break
        self._store.put_stack_check(repo, signature, reason)
        if reason:
            log.info("review.stacks %s: %s", repo, reason)
        return reason

    async def verify_all(self) -> int:
        """Check every stack whose heads weren't checked yet (after a sync), so the queue
        files a stack needing a restack before it is opened; return how many were checked.
        A stack git can't check is logged and left for the next run, never assumed."""
        stored = [s for s in self._store.prs(include_dismissed=True)]
        by_ref = {s.pr.ref: s for s in stored}
        checked = 0
        for chain in detect_stacks([s.pr for s in stored], self._store.restacks()).stacks:
            members = [by_ref[pr.ref] for pr in chain]
            repo = members[0].pr.ref.repo
            signature = stack_signature([(m.pr.ref.number, m.pr.head_sha) for m in members])
            if self._store.stack_check(repo, signature) is not None:
                continue
            try:
                await self._contained(members)
            except WorkspaceError as exc:
                log.warning("review.stacks %s left unchecked: %s", repo, exc)
                continue
            checked += 1
        return checked

    def start_verify(self) -> None:
        """Run ``verify_all`` in the background, one at a time."""
        if self._verify_task is not None and not self._verify_task.done():
            return
        self._verify_task = asyncio.create_task(self.verify_all(), name="review-stack-checks")

    async def stop_verify(self) -> None:
        """Cancel a running background check."""
        if self._verify_task is not None:
            self._verify_task.cancel()
            await asyncio.gather(self._verify_task, return_exceptions=True)
            self._verify_task = None

    # --- opening -----------------------------------------------------------------------------

    async def open(self, ref: PrRef, *, retry: bool = False) -> StackOpenResult:
        """Open the stack holding ``ref``: open each PR (its own round and jobs, under its own
        gates), then start the stack's jobs it lacks once no PR is held. A failed stack job
        runs again only with ``retry`` (R60).

        Raises:
            NotAStackError: as ``members``.
            PrNotInQueueError, ReviewNotAllowedError: a PR can't be opened.
        """
        members = self.members(ref)
        try:
            reason = await self._contained(members)
        except WorkspaceError as exc:
            raise NotAStackError(
                f"{ref}'s stack couldn't be checked against its heads, so nothing ran: {exc}"
            ) from exc
        if reason:
            # Never a stack review against a lower PR's stale code (R77).
            raise NotAStackError(f"{ref} isn't part of a stack: {reason}")
        opened = tuple([(m.pr.ref.number, await self._opener.open(m.pr.ref)) for m in members])
        repo = members[0].pr.ref.repo
        signature = stack_signature([(m.pr.ref.number, m.pr.head_sha) for m in members])
        stack_id = self._store.start_stack_review(repo, signature, self._clock())
        held = [(number, result) for number, result in opened if result.state not in _GOING]
        if held:
            why = "; ".join(f"#{number}: {result.reason}" for number, result in held)
            return StackOpenResult(
                "held", f"the stack's review waits for every PR: {why}", stack_id, (), opened
            )
        done = set(self._store.stack_artifacts(stack_id))
        failed = _failed(self._store.stack_artifact(stack_id, "jobs"))
        running = self._running.get(stack_id, set())
        missing = tuple(
            kind
            for kind in STACK_JOBS
            if kind.value not in done
            and kind not in running
            and (retry or kind.value not in failed)
        )
        if not missing:
            if running:
                return StackOpenResult(
                    "running", "the stack is being reviewed", stack_id, (), opened
                )
            return StackOpenResult(
                "up_to_date", "the stack's review is complete", stack_id, (), opened
            )
        self._running.setdefault(stack_id, set()).update(missing)
        task = asyncio.create_task(
            self._run(stack_id, members, missing), name=f"review-stack:{repo}:{stack_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return StackOpenResult("started", "reviewing the stack", stack_id, missing, opened)

    async def drain(self) -> None:
        """Wait for every stack review ``open`` started to end."""
        while self._tasks:
            tasks = list(self._tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)

    def recover(self) -> int:
        """At start (R73): stack jobs a stop or crash left ``running`` are marked
        ``interrupted``; they run again only through Retry. Returns how many stack reviews
        had one."""
        swept = 0
        for stack_id, stored in self._store.stack_jobs().items():
            jobs = stored if isinstance(stored, Mapping) else {}
            cut = [
                k for k, e in jobs.items() if isinstance(e, Mapping) and e.get("state") == "running"
            ]
            if not cut:
                continue
            record = dict(jobs)
            for kind in cut:
                record[kind] = {"state": "interrupted", "error": INTERRUPTED}
            self._store.put_stack_artifact(stack_id, "jobs", record)
            swept += 1
        return swept

    async def stop(self) -> None:
        """Cancel running stack jobs; the ones cut short are marked ``interrupted`` (R73)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _record(self, stack_id: int, kind: JobKind, state: str, error: str | None) -> None:
        jobs = self._store.stack_artifact(stack_id, "jobs")
        record = dict(jobs) if isinstance(jobs, Mapping) else {}
        record[kind.value] = {"state": state, "error": error}
        self._store.put_stack_artifact(stack_id, "jobs", record)

    async def _run(
        self, stack_id: int, members: Sequence[StoredPr], kinds: tuple[JobKind, ...]
    ) -> None:
        remaining = list(kinds)

        async def work() -> None:
            for kind in kinds:
                self._record(stack_id, kind, "running", None)
            try:
                stack, diffs = await self._prepare(stack_id, members)
            except Exception as exc:  # every stack job ends, with the cause, whatever failed
                log.warning("preparing stack review %d failed: %s", stack_id, exc, exc_info=exc)
                for kind in kinds:
                    self._record(stack_id, kind, "failed", str(exc) or type(exc).__name__)
                remaining.clear()
                return
            for kind in kinds:
                await self._job(stack, diffs, kind)
                remaining.remove(kind)

        try:
            await self._slots.run_open(work)
        except asyncio.CancelledError:
            for kind in remaining:  # cut short by a stop: never left "running" (R73)
                self._record(stack_id, kind, "interrupted", INTERRUPTED)
            raise
        finally:
            self._running[stack_id] = self._running.get(stack_id, set()) - set(kinds)
            await self._progress(members, stack_id)

    async def _job(self, stack: StackRound, diffs: Mapping[int, ParsedDiff], kind: JobKind) -> None:
        try:
            output = await self._jobs.run_stack(kind, stack)
        except JobFailed as exc:
            log.warning("stack review %d %s job failed: %s", stack.id, kind.value, exc)
            self._record(stack.id, kind, "failed", str(exc))
            return
        if kind is JobKind.STACK_WALKTHROUGH:

            def locate(number: int, path: str, hunk: int) -> bool:
                diff = diffs.get(number)
                return diff is not None and diff.locate(path, hunk) is not None

            numbers = {member.pr.number for member in stack.members}
            output = known_stack_walkthrough(output, numbers, locate)
        try:
            self._store.put_stack_artifact(stack.id, kind.value, output)
        except Exception as exc:  # this job fails alone, with its cause; the others go on
            log.warning("stack review %d %s result not stored: %s", stack.id, kind.value, exc,
                        exc_info=exc)  # fmt: skip
            self._record(stack.id, kind, "failed", str(exc) or type(exc).__name__)
            return
        self._record(stack.id, kind, "done", None)

    async def _prepare(
        self, stack_id: int, members: Sequence[StoredPr]
    ) -> tuple[StackRound, dict[int, ParsedDiff]]:
        """Each PR as GitHub has it now (still at the head the stack was read at), its
        commits, merge base, diff and hunk index (none for a diff too large or unreadable).

        Raises:
            StackPreparationError: a PR moved on since the stack was read.
            GitHubError, WorkspaceError: GitHub or git failed.
        """
        repo = members[0].pr.ref.repo
        await self._workspace.ensure_mirror(repo)
        viewer = self._store.viewer()
        rounds: list[Round] = []
        diffs: dict[int, ParsedDiff] = {}
        for member in members:
            snapshot = member.pr
            number = snapshot.ref.number
            pull = await self._github.get(repo, number)
            if pull.head_sha != snapshot.head_sha:
                raise StackPreparationError(
                    f"#{number} moved on to {pull.head_sha[:12]} since the stack was read; "
                    "open the stack again to review its new heads"
                )
            pull, commits = await self._github.commits_of(pull)
            await self._workspace.fetch(repo, [pull.base_sha, pull.head_sha])
            base = await self._workspace.merge_base(repo, pull.base_sha, pull.head_sha)
            index: HunkIndex | None = None
            try:
                text = await self._workspace.diff(repo, base, pull.head_sha)
                diff = await asyncio.to_thread(parse_unified_diff, text)
                diffs[number] = diff
                index = await asyncio.to_thread(hunk_index, diff)
            except (DiffTooLargeError, DiffParseError) as exc:
                log.warning("stack review %d: #%d's diff wasn't read: %s", stack_id, number, exc)
            latest = self._store.latest_round(snapshot.ref)
            rounds.append(
                Round(
                    0 if latest is None else latest.id,
                    pull,
                    tuple(commits),
                    snapshot.depth if latest is None else latest.depth,
                    merge_base=base,
                    hunk_index=index,
                    own_pr=author_kind(pull.author, pull.author_is_bot, viewer) == "you",
                )
            )
        own = all(r.own_pr for r in rounds)
        paths = list(dict.fromkeys(f.path for diff in diffs.values() for f in diff.files))
        stack = StackRound(stack_id, repo, tuple(rounds), self._summary(repo, paths), own)
        return stack, diffs

    def _summary(self, repo: str, paths: Sequence[str]) -> str:
        """The knowledge summary over every PR's paths; the model is secondary, so a failure
        leaves it out (logged with its cause)."""
        try:
            return self._knowledge.summary(repo, paths)
        except Exception as exc:  # secondary: the stack review goes on without it
            log.warning("reading the knowledge model for %s failed: %s", repo, exc, exc_info=exc)
            return ""

    async def _progress(self, members: Sequence[StoredPr], stack_id: int) -> None:
        """One ``review.progress`` event (ids only) so open views read the stack again."""
        top = members[-1].pr
        await self._bus.publish(
            Event(
                PROGRESS_TOPIC,
                {
                    "repo": top.ref.repo,
                    "number": top.ref.number,
                    "round_id": None,
                    "head_sha": top.head_sha,
                    "state": "finished",
                    "job": None,
                    "done": 0,
                    "total": 0,
                    "stack_id": stack_id,
                },
            )
        )

    # --- Post all ----------------------------------------------------------------------------

    def _check(self, ref: PrRef, item: PostAllItem) -> RoundRecord:
        """The item's round, if it can still be posted from (store only).

        Raises:
            NoReviewRound: no such round.
            RoundFrozen: a round on the PR's new head took it over.
            RoundChanged: it isn't the PR's current round.
        """
        found = self._store.round(item.round_id)
        if found is None:
            raise NoReviewRound(f"no review round {item.round_id} for {ref}")
        if found.frozen_at is not None:
            raise RoundFrozen(found.id, found.successor_id)
        latest = self._store.latest_round(ref)
        if latest is None or latest.id != found.id:
            raise RoundChanged(
                f"round {item.round_id} isn't {ref}'s current review round; nothing was posted"
            )
        return found

    def _earlier(self, item: PostAllItem, key: str) -> PostOutcome | None:
        """The item's outcome when its round was posted already (reporting only; the post
        itself re-checks under the round's lock): ``posted`` by this request's attempt with
        this digest and event, else ``already_posted`` with the event that went out (another
        way, or another version under this key, R47)."""
        mine = attempt_key(key, item.number, item.round_id)
        posted = [p for p in self._store.postings(item.round_id) if p.state == "posted"]
        for p in posted:
            if p.key == mine and p.digest == item.digest and p.event == item.event:
                return PostOutcome(item.number, item.round_id, p.event, "posted", p.review_url)
        if posted:
            last = posted[-1]
            return PostOutcome(
                item.number, item.round_id, last.event, "already_posted", last.review_url
            )
        return None

    async def post_all(
        self, ref: PrRef, items: Sequence[PostAllItem], *, key: str
    ) -> PostAllResult:
        """Post every item's confirmed review, bottom-up, one at a time; stop at the first
        failure. ``key``: the request's idempotency key, from which each PR's attempt key
        comes, so a replay never posts a PR twice. Every warning must be in the item's
        ``confirm``, as the author confirmed it: nothing is confirmed for them.

        Raises:
            NotAStackError: as ``members``.
            InvalidPostAllError: no items, a PR outside the stack, or a PR named twice.
            RoundNotOfPr: an item names a round of another PR; nothing was posted.
            PostAllStopped: a PR couldn't be posted (a frozen or stale round is found before
                anything is posted); its ``outcomes`` say what was and wasn't posted.
        """
        members = self.members(ref)
        repo = members[0].pr.ref.repo
        ordered = order_post_all(items, [m.pr.ref.number for m in members])
        for item in ordered:
            # Before the posted fast path: another PR's posted round must never make this PR
            # look posted (Codex P2 on PR #63). A round that doesn't exist is checked later.
            found = self._store.round(item.round_id)
            if found is not None and found.ref != PrRef(repo, item.number):
                raise RoundNotOfPr(item.number, item.round_id)
        # A frozen or stale round anywhere stops the run before anything is posted; a round
        # posted already (by this request or not) is reported, never checked or sent again.
        for at, item in enumerate(ordered):
            if self._earlier(item, key) is not None:
                continue
            try:
                self._check(PrRef(repo, item.number), item)
            except _POST_ERRORS as exc:
                raise PostAllStopped(self._stopped(ordered, at, exc, key), exc) from exc
        outcomes: list[PostOutcome] = []
        for at, item in enumerate(ordered):
            earlier = self._earlier(item, key)
            if earlier is not None:
                outcomes.append(earlier)
                continue
            try:
                self._check(PrRef(repo, item.number), item)
                url, state = await self._poster.post_unless_posted(
                    item.round_id,
                    item.event,
                    item.confirm,
                    key=attempt_key(key, item.number, item.round_id),
                    digest=item.digest,
                )
            except _POST_ERRORS as exc:
                stopped = [*outcomes, *self._stopped(ordered[at:], 0, exc, key)]
                raise PostAllStopped(stopped, exc) from exc
            log.info("review.post_all %s#%d round=%d %s", repo, item.number, item.round_id, state)
            outcomes.append(PostOutcome(item.number, item.round_id, item.event, state, url))
        return PostAllResult(tuple(outcomes))

    def _stopped(
        self, items: Sequence[PostAllItem], at: int, cause: Exception, key: str
    ) -> list[PostOutcome]:
        """Outcomes when the run stops at ``items[at]``: that one failed; every other one
        posted already (by this request or not) says so, and the rest weren't attempted."""
        out: list[PostOutcome] = []
        for index, item in enumerate(items):
            earlier = self._earlier(item, key)
            if index == at:
                out.append(
                    PostOutcome(
                        item.number,
                        item.round_id,
                        item.event,
                        "failed",
                        error=str(cause),
                        error_code=type(cause).__name__,
                    )
                )
            elif earlier is not None:
                out.append(earlier)
            else:
                out.append(PostOutcome(item.number, item.round_id, item.event, "not_attempted"))
        return out
