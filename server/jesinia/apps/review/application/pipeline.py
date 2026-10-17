# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review pipeline (spec §2 "Pre-review timing", §3): what a round runs, and when.

Two ways in, both on a PR's cached head:

- ``prereview`` is the scheduler's runner. The scheduler has already applied the gates; this
  runs the background jobs the pre-review setting asks for (``triage``: correctness,
  findings and the recommendation; ``everything``: those plus the on-open jobs; ``on_open``:
  nothing, unless the author loaded the PR) and then the evidence rule.
- ``open`` is the author opening the PR. It applies the same gates first (without the
  debounce: the author is looking now) and starts nothing unless the head is ready; only
  ``review_anyway`` skips them. Otherwise it starts whatever the round is missing: the
  on-open jobs, plus the triage jobs when they haven't run on this head.

Preparing a round reads the PR, its commits and its CI from GitHub; fetches the head and base
into the local mirror (with the default branch, whose review standards the jobs read); and
diffs merge base to head locally. Coverage always comes from that local diff (GitHub's lists
stop at 3,000 files and 250 commits); a diff over the workspace's byte cap is not read, and
coverage then says it is incomplete. Every degraded input is a warning on the round, and a
job that fails is named in the verdict's reasons; neither stops the other jobs.

Progress goes out on the ``review.progress`` topic, as ids, states and counts only: never the
PR's text, which is the PR author's.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal, Protocol

from jesinia.apps.review.application.errors import (
    GitHubBusyError,
    HeadMovedOnError,
    JobFailed,
    RoundInterruptedError,
    RoundPreparationError,
)
from jesinia.apps.review.application.jobs import CORRECTNESS_READS
from jesinia.apps.review.application.ports import PipelineStore
from jesinia.apps.review.application.scheduler import (
    PreReviewRequest,
    gate_for,
    resume_or_start_round,
)
from jesinia.apps.review.domain.coverage import (
    Coverage,
    DiffParseError,
    ParsedDiff,
    checked_coverage,
    coverage,
    hunk_index,
    parse_unified_diff,
    ranges_from_json,
    skips_from_json,
    steps_from_json,
    verified_ranges,
)
from jesinia.apps.review.domain.gates import Depth, GateState, propose_depth
from jesinia.apps.review.domain.jobs import JobKind, Round, recommend
from jesinia.apps.review.domain.queue import (
    PreReviewMode,
    PrHeadMovedError,
    PrNotInQueueError,
    PrRef,
    QueueDomainError,
    ReviewNotAllowedError,
    StoredPr,
)
from jesinia.apps.review.domain.workspace import DiffTooLargeError, WorkspaceError
from jesinia.core.events import Event, EventBus
from jesinia.services.github.application.prs import GitHubPayloadError
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubRequestError,
)
from jesinia.services.github.domain.pr import (
    CheckState,
    PrCommit,
    PrFile,
    PullRequest,
    PullRequestDomainError,
)

__all__ = [
    "ON_OPEN_JOBS",
    "PROGRESS_TOPIC",
    "TRIAGE_JOBS",
    "OpenResult",
    "Pipeline",
    "background_jobs",
]

log = logging.getLogger(__name__)

PROGRESS_TOPIC: Final = "review.progress"
# Spec §2: triage runs the recommendation, correctness pass and findings (and the depth,
# which Jesinia computes) in the background; the rest generates when the PR is opened.
TRIAGE_JOBS: Final[tuple[JobKind, ...]] = (
    JobKind.CORRECTNESS,
    JobKind.FINDINGS,
    JobKind.RECOMMENDATION,
)
# What opening adds, per depth (spec §3.1): a quick review is an explanation and a guided
# walk; a deep one has every tab. Stack walkthroughs and re-reviews come with plan Tasks 12/13.
ON_OPEN_JOBS: Final[Mapping[Depth, tuple[JobKind, ...]]] = {
    "quick": (JobKind.OVERVIEW, JobKind.WALKTHROUGH),
    "deep": (
        JobKind.OVERVIEW,
        JobKind.BACKGROUND,
        JobKind.WALKTHROUGH,
        JobKind.METRICS,
        JobKind.QUIZ,
    ),
}
_GITHUB_ERRORS = (GitHubError, GitHubPayloadError, PullRequestDomainError)


def _transient(exc: BaseException) -> bool:
    """A GitHub failure that says nothing about the PR: a rate limit, a 5xx, a timeout or a
    dropped connection (no status)."""
    if isinstance(exc, GitHubRateLimitedError):
        return True
    return isinstance(exc, GitHubRequestError) and (exc.status is None or exc.status >= 500)


def _github_failure(message: str, exc: Exception) -> RoundPreparationError:
    if _transient(exc):
        return GitHubBusyError(f"{message}; it is retried later", exc)
    return RoundPreparationError(message, exc)


Clock = Callable[[], datetime]
# The author's knowledge of a repo, from the knowledge model (plan Task 14). Trusted
# context for the agents, so it is built from Jesinia's own records only, never PR text.
KnowledgeSource = Callable[[str], str]
OpenState = Literal["started", "running", "up_to_date", "failed"] | GateState
ProgressState = Literal["started", "job_started", "job_done", "job_failed", "finished", "failed"]


def _failed_jobs(jobs: object) -> set[str]:
    """The kinds a round's ``jobs`` record marks failed."""
    entries = jobs.items() if isinstance(jobs, Mapping) else ()
    return {
        str(kind)
        for kind, entry in entries
        if isinstance(entry, Mapping) and entry.get("state") == "failed"
    }


def background_jobs(mode: PreReviewMode, depth: Depth, *, loaded: bool) -> tuple[JobKind, ...]:
    """The jobs a pre-review runs for a setting. A loaded PR in ``on_open`` mode still gets
    its triage: loading it asked for it now."""
    if mode == "everything":
        return (*TRIAGE_JOBS, *ON_OPEN_JOBS[depth])
    if mode == "triage" or loaded:
        return TRIAGE_JOBS
    return ()


def _nothing_known(repo: str) -> str:
    return ""


class PipelineGitHub(Protocol):
    """The GitHub reads a round needs (``services.github`` ``PrClient``); each may raise
    ``GitHubError`` subclasses."""

    async def get(self, repo: str, number: int) -> PullRequest: ...

    async def commits_of(self, pr: PullRequest) -> tuple[PullRequest, list[PrCommit]]: ...

    async def checks(self, repo: str, sha: str) -> CheckState: ...


class PrFileSource(Protocol):
    """GitHub's file list for a head, cached (``QueueService.files``); raises
    ``PrHeadMovedError`` when the PR is no longer at ``head_sha``."""

    async def files(self, repo: str, number: int, head_sha: str) -> list[PrFile]: ...


class PipelineWorkspace(Protocol):
    """The part of the repo workspace a round prepares with."""

    async def ensure_mirror(self, repo: str) -> None: ...

    async def fetch(self, repo: str, refs: list[str]) -> None: ...

    async def fetch_default_branch(self, repo: str) -> str: ...

    async def merge_base(self, repo: str, a: str, b: str) -> str: ...

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str: ...


class PipelineJobs(Protocol):
    """The agent jobs (``JobRunner``)."""

    async def run(self, job: JobKind, round: Round) -> dict[str, object]: ...

    async def correctness_depth(self, round: Round) -> Depth: ...


class RoundSlots(Protocol):
    """The scheduler's slots, which opened rounds share with pre-reviews (R52)."""

    async def run_open[T](self, work: Callable[[], Awaitable[T]]) -> T: ...


class _Unlimited:
    async def run_open[T](self, work: Callable[[], Awaitable[T]]) -> T:
        return await work()


@dataclass(frozen=True)
class OpenResult:
    """What opening a PR did: ``started`` the listed jobs on ``round_id`` (joining any
    already running); found everything it lacks already planned (``running``); found the
    round ``up_to_date``; or a gate state (``ci_failing``, ``waiting_ci``…) with its reason,
    the UI showing **Review anyway**, and nothing started."""

    state: OpenState
    reason: str
    round_id: int | None = None
    jobs: tuple[JobKind, ...] = ()


@dataclass(frozen=True)
class _Prepared:
    """A round ready for its jobs."""

    ref: PrRef
    round: Round
    diff: ParsedDiff
    ci: CheckState | None
    correctness_depth: Depth
    warnings: tuple[str, ...]


class _SingleFlight[K, T]:
    """At most one run per key: a caller that finds one running joins it (shares its
    result or error) instead of starting another."""

    def __init__(self) -> None:
        self._running: dict[K, asyncio.Future[T]] = {}

    def running(self, key: K) -> bool:
        return key in self._running

    async def run(self, key: K, make: Callable[[], Awaitable[T]]) -> T:
        joined = self._running.get(key)
        if joined is not None:
            return await asyncio.shield(joined)
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        # Mark an error as seen even when nobody joined, so asyncio doesn't log it.
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        self._running[key] = future
        try:
            value = await make()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(value)
            return value
        finally:
            del self._running[key]


class Pipeline:
    """Runs review rounds.

    Every job runs at most once per round (ruling R40): a job whose artifact is stored is
    done, also after a restart; a job that is running is joined, never started again; and
    the runners of one round share its preparation (one fetch, one diff).

    Args:
        github: PR, commit and CI reads (``PrClient``).
        files: GitHub's cached file lists (``QueueService``).
        workspace: the local mirror and diffs (``GitWorkspace``).
        jobs: the agent jobs (``JobRunner``).
        store: ``review.sqlite``.
        bus: where ``review.progress`` events go.
        clock: the current UTC time.
        knowledge: the author's familiarity with a repo, from the knowledge model.
        slots: the scheduler, whose round slots opens wait for; without it opens run at
            once.
    """

    def __init__(
        self,
        *,
        github: PipelineGitHub,
        files: PrFileSource,
        workspace: PipelineWorkspace,
        jobs: PipelineJobs,
        store: PipelineStore,
        bus: EventBus,
        clock: Clock,
        knowledge: KnowledgeSource = _nothing_known,
        slots: RoundSlots | None = None,
    ) -> None:
        self._slots: RoundSlots = _Unlimited() if slots is None else slots
        self._github = github
        self._files = files
        self._workspace = workspace
        self._jobs = jobs
        self._store = store
        self._bus = bus
        self._clock = clock
        self._knowledge = knowledge
        self._job_flights: _SingleFlight[tuple[int, JobKind], dict[str, object] | None] = (
            _SingleFlight()
        )
        self._recommend_flights: _SingleFlight[int, str] = _SingleFlight()
        self._prepare_flights: _SingleFlight[int, _Prepared] = _SingleFlight()
        # A round's preparation, kept while any runner of it is active.
        self._prepared: dict[int, _Prepared] = {}
        self._runners: dict[int, int] = {}
        # The jobs each round's active runners will still run, counted per runner.
        self._planned: dict[int, Counter[JobKind]] = {}
        self._opens: set[asyncio.Task[None]] = set()

    # --- the scheduler's runner ----------------------------------------------------------

    async def prereview(self, request: PreReviewRequest) -> None:
        """Run a background pre-review of ``request``'s round (the scheduler finishes it).

        Raises:
            RoundPreparationError: the round couldn't be prepared (``HeadMovedOnError``
                when the PR has moved on from the round's head).
        """
        if not background_jobs(request.mode, "quick", loaded=request.loaded):
            return  # "on open" and not loaded: nothing runs until the author opens it
        ref, head, round_id = request.ref, request.head_sha, request.round_id
        self._runners[round_id] = self._runners.get(round_id, 0) + 1
        # Planned at once, so an open meanwhile knows these are coming (the deep ones
        # once the depth is known).
        planned = background_jobs(request.mode, request.depth, loaded=request.loaded)
        self._plan(round_id, planned, +1)
        try:
            await self._progress(ref, round_id, head, "started")
            prepared = await self._prepare_or_report(ref, head, round_id)
            kinds = background_jobs(request.mode, prepared.round.depth, loaded=request.loaded)
            self._plan(round_id, kinds, +1)
            self._plan(round_id, planned, -1)
            planned = kinds
            await self._run(prepared, kinds)
        finally:
            self._plan(round_id, planned, -1)
            self._leave(round_id)

    # --- opening a PR --------------------------------------------------------------------

    async def open(
        self, ref: PrRef, *, depth: Depth | None = None, walk_everything: bool = False
    ) -> OpenResult:
        """The author opens ``ref``: gate it, then start what its round is missing.

        ``depth``: the author's choice (spec §3.1, switch either way), kept on the round and
        the queue row in place of Jesinia's proposal; its jobs are what opening runs.
        ``walk_everything``: run the walkthrough again, leaving nothing out (spec §3.1).

        Raises:
            PrNotInQueueError: the PR isn't cached (load it first).
            ReviewNotAllowedError: it is in a suggested repo and wasn't loaded, or GitHub
                couldn't load it on the last sync.
        """
        stored = self._consented(ref)
        if depth is not None:
            # Stored before the gate answers (M2): Review anyway later reviews at this depth.
            self._store.choose_depth(ref, stored.pr.head_sha, depth)
        config = self._store.repo_configs().get(ref.repo)
        result = gate_for(stored, config, self._clock(), immediate=True)
        if result.state != "ready" and not self._gate_passed(stored):
            return OpenResult(result.state, result.reason)
        return self._start_open(stored, depth=depth, walk_everything=walk_everything)

    async def retry(self, ref: PrRef) -> OpenResult:
        """The author's **Retry**: ``open``, but a failed round on the head is replaced by a
        new one (opening alone never reruns it, R60).

        Raises:
            PrNotInQueueError, ReviewNotAllowedError: as ``open``.
        """
        stored = self._consented(ref)
        config = self._store.repo_configs().get(ref.repo)
        result = gate_for(stored, config, self._clock(), immediate=True)
        if result.state != "ready" and not self._gate_passed(stored):
            return OpenResult(result.state, result.reason)
        return self._start_open(stored, rerun_failed=True)

    async def review_anyway(self, ref: PrRef) -> OpenResult:
        """``open`` without the CI and automated-review gates (the author's **Review
        anyway**). The evidence rule still applies: failing CI never approves.

        Raises:
            PrNotInQueueError, ReviewNotAllowedError: as ``open``; a PR GitHub couldn't
                load is reloaded, never reviewed from stale data.
        """
        stored = self._consented(ref)
        if stored.fetch_error is not None:
            raise ReviewNotAllowedError(
                f"{ref} couldn't be loaded from GitHub ({stored.fetch_error}); reload it first"
            )
        self._store.override_gates(ref, stored.pr.head_sha)
        return self._start_open(stored, rerun_failed=True)

    def _gate_passed(self, stored: StoredPr) -> bool:
        """Whether the author chose **Review anyway** for this head (R66): only then do later
        opens, Retry, Walk everything and a depth switch go past the CI and automated-review
        gates. A round on the head is not enough (CI may have turned failing since), a new
        head has no override, and a fetch failure still holds it."""
        pr = stored.pr
        return stored.fetch_error is None and self._store.gates_overridden(pr.ref, pr.head_sha)

    def _consented(self, ref: PrRef) -> StoredPr:
        stored = self._store.pr(ref)
        if stored is None:
            raise PrNotInQueueError(f"{ref} is not in the review queue; load it first")
        config = self._store.repo_configs().get(ref.repo)
        if config is not None and config.suggested and not stored.loaded:
            raise ReviewNotAllowedError(
                f"{ref.repo} is a suggested repo: load {ref} (or confirm the repo) to review it"
            )
        return stored

    def _start_open(
        self,
        stored: StoredPr,
        *,
        depth: Depth | None = None,
        walk_everything: bool = False,
        rerun_failed: bool = False,
    ) -> OpenResult:
        pr = stored.pr
        ref, head = pr.ref, pr.head_sha
        latest = self._store.latest_round_on_head(ref, head)
        reusable = latest is not None and (not latest[1].finished or latest[1].error is None)
        if latest is not None and not reusable and not latest[1].interrupted and not rerun_failed:
            # A look (a tab switch, a reload) never starts a new agent round: Retry does. An
            # interrupted round isn't a failure: it is resumed (R51).
            cause = latest[1].error or "no cause recorded"
            return OpenResult(
                "failed", f"the last review of this head failed: {cause}; Retry runs it again",
                latest[0],
            )  # fmt: skip
        if latest is not None and reusable:
            round_id, owned = latest[0], False
        else:  # an interrupted round is resumed (R51); a failed one is replaced
            round_id = resume_or_start_round(self._store, stored, self._clock())
            owned = True
        if depth is not None:
            self._store.set_depth(ref, head, round_id, depth)
            # A round prepared at the old depth is cached while it runs: the jobs this switch
            # adds must see the new one (Codex round 2 on PR #58).
            cached = self._prepared.get(round_id)
            if cached is not None and cached.round.depth != depth:
                self._prepared[round_id] = replace(cached, round=replace(cached.round, depth=depth))
        done = self._store.artifact_kinds(round_id)
        wanted = (*TRIAGE_JOBS, *ON_OPEN_JOBS[depth or pr.depth])
        missing = tuple(kind for kind in wanted if kind.value not in done)
        if not rerun_failed:
            # A failed job reruns only through Retry, like a failed round (R60, final M1).
            failed = _failed_jobs(self._store.artifact(round_id, "jobs"))
            missing = tuple(kind for kind in missing if kind.value not in failed)
        coming = self._planned.get(round_id, Counter())
        if walk_everything and (
            coming[JobKind.WALKTHROUGH]
            or self._job_flights.running((round_id, JobKind.WALKTHROUGH))
        ):
            # Joining the running walk would walk nothing more (final M6): say so instead.
            return OpenResult(
                "running", "the walkthrough is running; walk everything once it finishes", round_id
            )
        rerun = (JobKind.WALKTHROUGH,) if walk_everything else ()
        missing = (*missing, *(kind for kind in rerun if kind not in missing))
        if not missing:
            return OpenResult("up_to_date", "this head's review is complete", round_id)
        if not rerun and all(coming[kind] for kind in missing):
            return OpenResult("running", "this head is being reviewed", round_id)
        self._plan(round_id, missing, +1)
        if owned:
            self._store.set_state(ref, head, "running", "reviewing (opened)")
        # Counted now, so a runner finishing meanwhile can't drop the shared preparation.
        self._runners[round_id] = self._runners.get(round_id, 0) + 1
        task = asyncio.create_task(
            self._run_open(ref, head, round_id, missing, owned, frozenset(rerun)),
            name=f"review-open:{ref}",
        )
        self._opens.add(task)
        task.add_done_callback(self._opens.discard)
        return OpenResult("started", "reviewing", round_id, missing)

    async def _run_open(
        self,
        ref: PrRef,
        head: str,
        round_id: int,
        kinds: tuple[JobKind, ...],
        owned: bool,
        rerun: frozenset[JobKind] = frozenset(),
    ) -> None:
        verdict: str | None = None

        async def review() -> str | None:
            await self._progress(ref, round_id, head, "started")
            prepared = await self._prepare_or_report(ref, head, round_id)
            return await self._run(prepared, kinds, rerun)

        try:
            verdict = await self._slots.run_open(review)
        except asyncio.CancelledError:
            if owned:
                cause = "Jesinia stopped during this review"
                self._store.finish_round(round_id, self._clock(), cause, interrupted=True)
                self._store.set_state(ref, head, "queued", "interrupted by a stop")
            raise
        except RoundInterruptedError as exc:  # as the scheduler does (M13): not a failure
            if owned:
                self._store.finish_round(
                    round_id, self._clock(), str(exc), interrupted=True, counted=False
                )
                self._store.set_state(ref, head, "queued", str(exc))
        except Exception as exc:  # the round must end, with its cause, whatever went wrong
            log.warning("review of %s failed: %s", ref, exc, exc_info=exc)
            if owned:
                self._store.finish_round(round_id, self._clock(), str(exc) or type(exc).__name__)
                self._store.set_state(ref, head, "failed", f"review failed: {exc}")
        else:
            if owned:
                self._store.finish_round(round_id, self._clock(), None, verdict)
                self._store.set_state(ref, head, "ready", "review finished")
        finally:
            self._plan(round_id, kinds, -1)
            self._leave(round_id)

    async def drain(self) -> None:
        """Wait for every review started by ``open`` to end."""
        while self._opens:
            tasks = list(self._opens)
            await asyncio.gather(*tasks, return_exceptions=True)
            # Done callbacks run later; awaiting finished tasks again would never yield.
            self._opens.difference_update(tasks)

    async def stop(self) -> None:
        """Cancel running opens; their rounds are marked interrupted."""
        tasks = list(self._opens)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- preparing a round ---------------------------------------------------------------

    async def _prepare_or_report(self, ref: PrRef, head: str, round_id: int) -> _Prepared:
        """The round's shared preparation (made once while it has runners); any failure is
        published as ``failed`` before it is raised."""
        try:
            prepared = self._prepared.get(round_id)
            if prepared is None:
                prepared = await self._prepare_flights.run(
                    round_id, lambda: self._prepare(ref, head, round_id)
                )
                self._prepared[round_id] = prepared
            return prepared
        except Exception:
            await self._progress(ref, round_id, head, "failed")
            raise

    def _plan(self, round_id: int, kinds: tuple[JobKind, ...], step: int) -> None:
        planned = self._planned.setdefault(round_id, Counter())
        planned.update({kind: step for kind in kinds})
        if not +planned:
            del self._planned[round_id]

    def _leave(self, round_id: int) -> None:
        left = self._runners.get(round_id, 1) - 1
        if left > 0:
            self._runners[round_id] = left
        else:
            self._runners.pop(round_id, None)
            self._prepared.pop(round_id, None)

    async def _prepare(self, ref: PrRef, head: str, round_id: int) -> _Prepared:
        repo, warnings = ref.repo, list[str]()
        try:
            pull = await self._github.get(repo, ref.number)
            if pull.head_sha != head:
                raise HeadMovedOnError(
                    f"{ref} moved on to {pull.head_sha[:12]} after this round started on "
                    f"{head[:12]}; the new head is reviewed once it settles"
                )
            pull, commits = await self._github.commits_of(pull)
        except _GITHUB_ERRORS as exc:
            raise _github_failure(f"couldn't read {ref} from GitHub", exc) from exc
        files_truncated = await self._files_truncated(ref, pull, warnings)
        pull = replace(pull, files_truncated=files_truncated)
        try:
            await self._workspace.ensure_mirror(repo)
            # SHAs only: a fetch must never name one of Jesinia's own refs/jesinia/ refs.
            await self._workspace.fetch(repo, [pull.base_sha, head])
            base = await self._workspace.merge_base(repo, pull.base_sha, head)
        except WorkspaceError as exc:
            raise RoundPreparationError(f"couldn't fetch {ref}'s commits", exc) from exc
        try:
            # The jobs read the repo's review standards from the default branch (R39).
            await self._workspace.fetch_default_branch(repo)
        except WorkspaceError as exc:
            warnings.append(
                "the default branch couldn't be refreshed, so the repo's review standards "
                f"may be out of date: {exc}"
            )
        diff = await self._diff(ref, pull, base, warnings)
        try:
            ci: CheckState | None = await self._github.checks(repo, head)
        except _GITHUB_ERRORS as exc:
            if _transient(exc):  # missing CI would decide the verdict: wait for GitHub
                raise _github_failure(f"couldn't read CI on {ref}", exc) from exc
            ci = None
            warnings.append(f"CI on this head couldn't be read, so it counts as missing: {exc}")
        proposed = propose_depth(pull.additions + pull.deletions, pull.changed_files, None)
        depth = self._store.chosen_depth(ref, head) or proposed
        index = await asyncio.to_thread(hunk_index, diff) if diff.complete else None
        round = Round(
            round_id,
            pull,
            tuple(commits),
            depth,
            knowledge=self._knowledge(repo),
            merge_base=base,
            hunk_index=index,
        )
        correctness_depth = await self._jobs.correctness_depth(round)
        self._store.set_depth(ref, head, round_id, depth)
        self._store.put_artifact(
            round_id,
            "depth",
            {
                "proposed": proposed,
                "correctness": correctness_depth,
                "lines": pull.additions + pull.deletions,
                "files": pull.changed_files,
            },
        )
        self._store.put_artifact(round_id, "warnings", warnings)
        return _Prepared(ref, round, diff, ci, correctness_depth, tuple(warnings))

    async def _files_truncated(self, ref: PrRef, pull: PullRequest, warnings: list[str]) -> bool:
        """Read (and cache) GitHub's file list for the head, and say whether it is short of
        the PR's file count. When the PR moves mid-read, read it again once, on whatever
        head it is at now; if that isn't this round's head, the round goes on from the
        local diff without a GitHub list, and says so."""
        try:
            try:
                files = await self._files.files(ref.repo, ref.number, pull.head_sha)
            except PrHeadMovedError:
                now = await self._github.get(ref.repo, ref.number)
                files = await self._files.files(ref.repo, ref.number, now.head_sha)
                if now.head_sha != pull.head_sha:
                    warnings.append(
                        f"the PR moved on to {now.head_sha[:12]} while its files were read; "
                        f"this round reviews {pull.head_sha[:12]} from the local diff, without "
                        "GitHub's list of its files"
                    )
                    return True
        except (*_GITHUB_ERRORS, QueueDomainError, WorkspaceError) as exc:
            if _transient(exc):  # GitHub's hiccup, not a short list: retry the round later
                raise _github_failure(f"couldn't read {ref}'s files", exc) from exc
            warnings.append(f"GitHub's list of the PR's files couldn't be read: {exc}")
            return True
        return len(files) < pull.changed_files

    async def _diff(
        self, ref: PrRef, pull: PullRequest, base: str, warnings: list[str]
    ) -> ParsedDiff:
        """The merge base to head diff, parsed off the event loop; incomplete (with GitHub's
        counts standing in for what is missing) when it is too large or unreadable."""
        try:
            text = await self._workspace.diff(ref.repo, base, pull.head_sha)
            return await asyncio.to_thread(parse_unified_diff, text)
        except (DiffTooLargeError, DiffParseError) as exc:
            unread: Exception = exc
        except WorkspaceError as exc:
            raise RoundPreparationError(f"couldn't diff {ref}", exc) from exc
        reason = str(unread)
        warnings.append(f"the diff is too large or unreadable, so it wasn't read: {reason}")
        return ParsedDiff(
            (),
            complete=False,
            incomplete_reason=reason,
            expected_lines=pull.additions + pull.deletions,
            expected_files=pull.changed_files,
        )

    # --- running jobs --------------------------------------------------------------------

    async def _run(
        self,
        prepared: _Prepared,
        kinds: tuple[JobKind, ...],
        rerun: frozenset[JobKind] = frozenset(),
    ) -> str | None:
        """Run ``kinds`` in order (triage first, then the evidence rule, then the rest);
        return the verdict, if the triage ran. ``rerun``: jobs run again even if done (the
        walkthrough, for "Walk everything")."""
        triage = tuple(kind for kind in kinds if kind in TRIAGE_JOBS)
        rest = tuple(kind for kind in kinds if kind not in TRIAGE_JOBS)
        progress = {"done": 0, "total": len(kinds)}
        for kind in triage:
            await self._job(prepared, kind, progress, kind in rerun)
        verdict = None
        if triage:
            verdict = await self._recommend_flights.run(
                prepared.round.id, lambda: self._recommend(prepared)
            )
        for later in rest:
            await self._job(prepared, later, progress, later in rerun)
        await self._progress(
            prepared.ref,
            prepared.round.id,
            prepared.round.pr.head_sha,
            "finished",
            done=progress["done"],
            total=progress["total"],
        )
        return verdict

    async def _job(
        self, prepared: _Prepared, kind: JobKind, progress: dict[str, int], again: bool = False
    ) -> None:
        """Run ``kind`` unless it is done (or ``again``: walking everything); join it if it
        is running (R40)."""
        round = prepared.round
        head = round.pr.head_sha
        counts = {"done": progress["done"], "total": progress["total"]}
        await self._progress(prepared.ref, round.id, head, "job_started", kind, **counts)
        key = (round.id, kind)
        running = self._job_flights.running(key)
        if not again and not running and kind.value in self._store.artifact_kinds(round.id):
            output: dict[str, object] | None = {}  # done earlier, maybe before a restart
        else:
            output = await self._job_flights.run(
                key, lambda: self._execute(prepared, kind, walk_everything=again)
            )
        progress["done"] += 1
        state: ProgressState = "job_failed" if output is None else "job_done"
        counts = {"done": progress["done"], "total": progress["total"]}
        await self._progress(prepared.ref, round.id, head, state, kind, **counts)

    async def _execute(
        self, prepared: _Prepared, kind: JobKind, *, walk_everything: bool = False
    ) -> dict[str, object] | None:
        """Run one job and record how it went (``review.sqlite`` keeps the state, so a
        restart knows what finished); ``None`` if it failed."""
        round_id = prepared.round.id
        self._record_job(round_id, kind, "running", None)
        try:
            round = replace(prepared.round, walk_everything=walk_everything)
            output = await self._jobs.run(kind, round)
        except JobFailed as exc:
            log.warning("review %s job failed on %s: %s", kind.value, prepared.ref, exc)
            self._record_job(round_id, kind, "failed", str(exc))
            return None
        if kind is JobKind.WALKTHROUGH:
            # Filtered at once, before anything else runs, so no reader sees invented hunks.
            self._store.put_artifact(round_id, kind.value, _known_steps(prepared.diff, output))
        self._record_job(round_id, kind, "done", None)
        if kind is JobKind.WALKTHROUGH:
            await self._walkthrough(prepared, output)
        return output

    def _record_job(self, round_id: int, kind: JobKind, state: str, error: str | None) -> None:
        jobs = self._store.artifact(round_id, "jobs")
        record = dict(jobs) if isinstance(jobs, Mapping) else {}
        record[kind.value] = {"state": state, "error": error}
        self._store.put_artifact(round_id, "jobs", record)

    async def _recommend(self, prepared: _Prepared) -> str:
        """Apply the evidence rule to the triage's artifacts; store the verdict, the
        evidence and the correctness pass's coverage."""
        round_id = prepared.round.id
        correctness = self._store.artifact(round_id, "correctness")
        found = self._store.artifact(round_id, "findings")
        findings = found.get("findings") if isinstance(found, Mapping) else None
        suggestion = self._store.artifact(round_id, "recommendation")
        read = claimed = None
        if isinstance(correctness, Mapping):
            # The pass's claim counts only where its tools returned those lines (M7).
            checked = ranges_from_json(correctness)
            record = self._store.artifact(round_id, CORRECTNESS_READS)
            verified = verified_ranges(checked, ranges_from_json(record, key="read"))
            claimed = await asyncio.to_thread(checked_coverage, prepared.diff, checked)
            read = await asyncio.to_thread(checked_coverage, prepared.diff, verified)
        result = recommend(
            prepared.ci,
            correctness if isinstance(correctness, Mapping) else None,
            findings if isinstance(findings, list) else None,
            suggestion.get("verdict") if isinstance(suggestion, Mapping) else None,
            depth=prepared.correctness_depth,
            coverage=read,
            claimed=claimed,
        )
        jobs = self._store.artifact(round_id, "jobs")
        failed = [
            str(entry.get("error"))
            for kind, entry in (jobs.items() if isinstance(jobs, Mapping) else ())
            if kind in {k.value for k in TRIAGE_JOBS}
            and isinstance(entry, Mapping)
            and entry.get("state") == "failed"
        ]
        reasons = [*result.reasons, *failed]
        self._store.put_artifact(round_id, "evidence", result.evidence.as_json())
        self._store.put_artifact(
            round_id, "verdict", {"verdict": result.verdict, "reasons": reasons}
        )
        self._store.set_verdict(round_id, result.verdict)
        self._put_coverage(prepared, correctness=read, claimed=claimed)
        return result.verdict

    async def _walkthrough(self, prepared: _Prepared, output: Mapping[str, object]) -> None:
        """Compute the walkthrough's coverage."""
        steps, skips = steps_from_json(output), skips_from_json(output)
        walked = await asyncio.to_thread(coverage, prepared.diff, steps, skips)
        self._put_coverage(prepared, walkthrough=walked)

    def _put_coverage(
        self,
        prepared: _Prepared,
        *,
        walkthrough: Coverage | None = None,
        correctness: Coverage | None = None,
        claimed: Coverage | None = None,
    ) -> None:
        """Merge into the round's ``coverage`` artifact (no await in between, so concurrent
        writers can't lose each other's part). Its ``pct``, which the queue row shows, is
        the walkthrough's once there is one, else the correctness pass's (what it verifiably
        read; ``correctness_claimed`` is what it said it checked). ``nothing_to_walk``: the
        diff changes no line, so the UI says so rather than showing 100% (M3)."""
        round_id, pr = prepared.round.id, prepared.round.pr
        old = self._store.artifact(round_id, "coverage")
        before = old if isinstance(old, Mapping) else {}
        walk = walkthrough.as_json() if walkthrough else before.get("walkthrough")
        read = correctness.as_json() if correctness else before.get("correctness")
        said = claimed.as_json() if claimed else before.get("correctness_claimed")
        headline = walk if isinstance(walk, Mapping) else read
        pct = headline.get("pct") if isinstance(headline, Mapping) else None
        self._store.put_artifact(
            round_id,
            "coverage",
            {
                "pct": pct,
                "source": "walkthrough" if isinstance(walk, Mapping) else "correctness",
                "complete": prepared.diff.complete,
                "incomplete_reason": prepared.diff.incomplete_reason,
                "files_truncated": pr.files_truncated,
                "commits_truncated": pr.commits_truncated,
                "nothing_to_walk": prepared.diff.complete and prepared.diff.changed_lines == 0,
                "walkthrough": walk,
                "correctness": read,
                "correctness_claimed": said,
            },
        )

    async def _progress(
        self,
        ref: PrRef,
        round_id: int,
        head: str,
        state: ProgressState,
        job: JobKind | None = None,
        *,
        done: int = 0,
        total: int = 0,
    ) -> None:
        """One ``review.progress`` event: ids, a state and counts, never PR text."""
        await self._bus.publish(
            Event(
                PROGRESS_TOPIC,
                {
                    "repo": ref.repo,
                    "number": ref.number,
                    "round_id": round_id,
                    "head_sha": head,
                    "state": state,
                    "job": None if job is None else job.value,
                    "done": done,
                    "total": total,
                },
            )
        )


def _known_steps(diff: ParsedDiff, output: Mapping[str, object]) -> dict[str, object]:
    """The walkthrough with only the steps whose every hunk is in ``diff``."""
    raw = output.get("steps")
    kept = [
        item
        for item, step in zip(
            raw if isinstance(raw, list) else [], steps_from_json(output), strict=False
        )
        if step.hunks and all(diff.locate(ref.path, ref.hunk) is not None for ref in step.hunks)
    ]
    return {**output, "steps": kept}
