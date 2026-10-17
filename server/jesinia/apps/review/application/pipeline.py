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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal, Protocol

from jesinia.apps.review.application.errors import (
    HeadMovedOnError,
    JobFailed,
    RoundPreparationError,
)
from jesinia.apps.review.application.ports import PipelineStore
from jesinia.apps.review.application.scheduler import PreReviewRequest, gate_for
from jesinia.apps.review.domain.coverage import (
    Coverage,
    DiffParseError,
    ParsedDiff,
    checked_coverage,
    coverage,
    parse_unified_diff,
    ranges_from_json,
    skips_from_json,
    steps_from_json,
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
from jesinia.services.github.application.service import GitHubError
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

Clock = Callable[[], datetime]
# The author's knowledge of a repo, from the knowledge model (plan Task 14). Trusted
# context for the agents, so it is built from Jesinia's own records only, never PR text.
KnowledgeSource = Callable[[str], str]
OpenState = Literal["started", "up_to_date"] | GateState
ProgressState = Literal["started", "job_started", "job_done", "job_failed", "finished", "failed"]


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


@dataclass(frozen=True)
class OpenResult:
    """What opening a PR did: ``started`` the listed jobs on ``round_id``; found the round
    ``up_to_date``; or a gate state (``ci_failing``, ``waiting_ci``…) with its reason, the
    UI showing **Review anyway**, and nothing started."""

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


class Pipeline:
    """Runs review rounds.

    Args:
        github: PR, commit and CI reads (``PrClient``).
        files: GitHub's cached file lists (``QueueService``).
        workspace: the local mirror and diffs (``GitWorkspace``).
        jobs: the agent jobs (``JobRunner``).
        store: ``review.sqlite``.
        bus: where ``review.progress`` events go.
        clock: the current UTC time.
        knowledge: the author's familiarity with a repo, from the knowledge model.
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
    ) -> None:
        self._github = github
        self._files = files
        self._workspace = workspace
        self._jobs = jobs
        self._store = store
        self._bus = bus
        self._clock = clock
        self._knowledge = knowledge
        # Jobs queued or running per round, so an open never repeats a pre-review's.
        self._in_flight: dict[int, set[JobKind]] = {}
        self._opens: set[asyncio.Task[None]] = set()

    # --- the scheduler's runner ----------------------------------------------------------

    async def prereview(self, request: PreReviewRequest) -> None:
        """Run a background pre-review of ``request``'s round (the scheduler finishes it).

        Raises:
            RoundPreparationError: the round couldn't be prepared (``HeadMovedOnError``
                when the PR has moved on from the round's head).
        """
        planned = background_jobs(request.mode, "quick", loaded=request.loaded)
        if not planned:
            return  # "on open" and not loaded: nothing runs until the author opens it
        # Reserved before preparing, so an open meanwhile leaves these jobs to this run.
        reserved = self._reserve(request.round_id, planned)
        try:
            await self._progress(request.ref, request.round_id, request.head_sha, "started")
            prepared = await self._prepare_or_report(
                request.ref, request.head_sha, request.round_id
            )
            kinds = background_jobs(request.mode, prepared.round.depth, loaded=request.loaded)
            reserved |= self._reserve(request.round_id, kinds)
            await self._run(prepared, kinds)
        finally:
            self._release(request.round_id, reserved)

    # --- opening a PR --------------------------------------------------------------------

    async def open(self, ref: PrRef) -> OpenResult:
        """The author opens ``ref``: gate it, then start what its round is missing.

        Raises:
            PrNotInQueueError: the PR isn't cached (load it first).
            ReviewNotAllowedError: it is in a suggested repo and wasn't loaded.
        """
        stored = self._consented(ref)
        config = self._store.repo_configs().get(ref.repo)
        result = gate_for(stored, config, self._clock(), immediate=True)
        if result.state != "ready":
            return OpenResult(result.state, result.reason)
        return self._start_open(stored)

    async def review_anyway(self, ref: PrRef) -> OpenResult:
        """``open`` without the gates (the author's **Review anyway**). The evidence rule
        still applies: failing CI never approves.

        Raises:
            PrNotInQueueError, ReviewNotAllowedError: as ``open``.
        """
        return self._start_open(self._consented(ref))

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

    def _start_open(self, stored: StoredPr) -> OpenResult:
        pr = stored.pr
        ref, head = pr.ref, pr.head_sha
        latest = self._store.latest_round_on_head(ref, head)
        reusable = latest is not None and (not latest[1].finished or latest[1].error is None)
        if latest is not None and reusable:
            round_id, owned = latest[0], False
        else:
            round_id = self._store.start_round(ref, head, pr.base_sha, pr.depth, self._clock())
            owned = True
        done = set(self._store.artifacts(round_id)) if not owned else set()
        busy = self._in_flight.get(round_id, set())
        wanted = (*TRIAGE_JOBS, *ON_OPEN_JOBS[pr.depth])
        missing = tuple(kind for kind in wanted if kind.value not in done and kind not in busy)
        if not missing:
            return OpenResult("up_to_date", "this head's review is complete", round_id)
        self._reserve(round_id, missing)
        if owned:
            self._store.set_state(ref, head, "running", "reviewing (opened)")
        task = asyncio.create_task(
            self._run_open(ref, head, round_id, missing, owned), name=f"review-open:{ref}"
        )
        self._opens.add(task)
        task.add_done_callback(self._opens.discard)
        return OpenResult("started", "reviewing", round_id, missing)

    async def _run_open(
        self, ref: PrRef, head: str, round_id: int, kinds: tuple[JobKind, ...], owned: bool
    ) -> None:
        verdict: str | None = None
        try:
            await self._progress(ref, round_id, head, "started")
            prepared = await self._prepare_or_report(ref, head, round_id)
            verdict = await self._run(prepared, kinds)
        except asyncio.CancelledError:
            if owned:
                cause = "Jesinia stopped during this review"
                self._store.finish_round(round_id, self._clock(), cause, interrupted=True)
                self._store.set_state(ref, head, "queued", "interrupted by a stop")
            raise
        except Exception as exc:  # the round must end, with its cause, whatever went wrong
            log.warning("review of %s failed: %s", ref, exc, exc_info=exc)
            if not isinstance(exc, RoundPreparationError):
                await self._progress(ref, round_id, head, "failed")
            if owned:
                self._store.finish_round(round_id, self._clock(), str(exc) or type(exc).__name__)
                self._store.set_state(ref, head, "failed", f"review failed: {exc}")
        else:
            if owned:
                self._store.finish_round(round_id, self._clock(), None, verdict)
                self._store.set_state(ref, head, "ready", "review finished")
        finally:
            self._release(round_id, set(kinds))

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
        try:
            return await self._prepare(ref, head, round_id)
        except RoundPreparationError:
            await self._progress(ref, round_id, head, "failed")
            raise

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
            raise RoundPreparationError(f"couldn't read {ref} from GitHub", exc) from exc
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
            ci = None
            warnings.append(f"CI on this head couldn't be read, so it counts as missing: {exc}")
        depth = propose_depth(pull.additions + pull.deletions, pull.changed_files, None)
        round = Round(round_id, pull, tuple(commits), depth, knowledge=self._knowledge(repo))
        correctness_depth = await self._jobs.correctness_depth(round)
        self._store.set_depth(ref, head, round_id, depth)
        self._store.put_artifact(
            round_id,
            "depth",
            {
                "proposed": depth,
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

    async def _run(self, prepared: _Prepared, kinds: tuple[JobKind, ...]) -> str | None:
        """Run ``kinds`` in order (triage first, then the evidence rule, then the rest);
        return the verdict, if the triage ran."""
        triage = tuple(kind for kind in kinds if kind in TRIAGE_JOBS)
        rest = tuple(kind for kind in kinds if kind not in TRIAGE_JOBS)
        progress = {"done": 0, "total": len(kinds)}
        failures: dict[JobKind, JobFailed] = {}
        for kind in triage:
            await self._job(prepared, kind, progress, failures)
        verdict = await self._recommend(prepared, failures) if triage else None
        for later in rest:
            output = await self._job(prepared, later, progress, failures)
            if later is JobKind.WALKTHROUGH and output is not None:
                await self._walkthrough(prepared, output)
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
        self,
        prepared: _Prepared,
        kind: JobKind,
        progress: dict[str, int],
        failures: dict[JobKind, JobFailed],
    ) -> dict[str, object] | None:
        round = prepared.round
        head = round.pr.head_sha
        await self._progress(
            prepared.ref,
            round.id,
            head,
            "job_started",
            kind,
            done=progress["done"],
            total=progress["total"],
        )
        try:
            output: dict[str, object] | None = await self._jobs.run(kind, round)
        except JobFailed as exc:
            log.warning("review %s job failed on %s: %s", kind.value, prepared.ref, exc)
            failures[kind] = exc
            output = None
        progress["done"] += 1
        self._release(round.id, {kind})
        self._record_job(round.id, kind, failures.get(kind))
        state: ProgressState = "job_failed" if kind in failures else "job_done"
        await self._progress(
            prepared.ref,
            round.id,
            head,
            state,
            kind,
            done=progress["done"],
            total=progress["total"],
        )
        return output

    def _record_job(self, round_id: int, kind: JobKind, failure: JobFailed | None) -> None:
        jobs = self._store.artifacts(round_id).get("jobs")
        record = dict(jobs) if isinstance(jobs, Mapping) else {}
        record[kind.value] = {
            "state": "failed" if failure else "done",
            "error": None if failure is None else str(failure),
        }
        self._store.put_artifact(round_id, "jobs", record)

    async def _recommend(self, prepared: _Prepared, failures: Mapping[JobKind, JobFailed]) -> str:
        """Apply the evidence rule to the triage's artifacts; store the verdict, the
        evidence and the correctness pass's coverage."""
        round_id = prepared.round.id
        artifacts = self._store.artifacts(round_id)
        correctness = artifacts.get("correctness")
        found = artifacts.get("findings")
        findings = found.get("findings") if isinstance(found, Mapping) else None
        suggestion = artifacts.get("recommendation")
        read = (
            await asyncio.to_thread(checked_coverage, prepared.diff, ranges_from_json(correctness))
            if isinstance(correctness, Mapping)
            else None
        )
        result = recommend(
            prepared.ci,
            correctness if isinstance(correctness, Mapping) else None,
            findings if isinstance(findings, list) else None,
            suggestion.get("verdict") if isinstance(suggestion, Mapping) else None,
            depth=prepared.correctness_depth,
            coverage=read,
        )
        reasons = [*result.reasons, *(str(failure) for failure in failures.values())]
        self._store.put_artifact(round_id, "evidence", result.evidence.as_json())
        self._store.put_artifact(
            round_id, "verdict", {"verdict": result.verdict, "reasons": reasons}
        )
        self._store.set_verdict(round_id, result.verdict)
        self._put_coverage(prepared, correctness=read)
        return result.verdict

    async def _walkthrough(self, prepared: _Prepared, output: Mapping[str, object]) -> None:
        """Compute the walkthrough's coverage, and keep only the steps whose hunks exist."""
        steps, skips = steps_from_json(output), skips_from_json(output)
        walked = await asyncio.to_thread(coverage, prepared.diff, steps, skips)
        known = {(f.path, h) for f in prepared.diff.files for h in range(len(f.hunks))}
        raw = output.get("steps")
        kept = [
            item
            for item, step in zip(raw if isinstance(raw, list) else [], steps, strict=False)
            if step.hunks and {(ref.path, ref.hunk) for ref in step.hunks} <= known
        ]
        self._store.put_artifact(prepared.round.id, "walkthrough", {**output, "steps": kept})
        self._put_coverage(prepared, walkthrough=walked)

    def _put_coverage(
        self,
        prepared: _Prepared,
        *,
        walkthrough: Coverage | None = None,
        correctness: Coverage | None = None,
    ) -> None:
        """Merge into the round's ``coverage`` artifact (no await in between, so concurrent
        writers can't lose each other's part). Its ``pct``, which the queue row shows, is
        the walkthrough's once there is one, else the correctness pass's."""
        round_id, pr = prepared.round.id, prepared.round.pr
        old = self._store.artifacts(round_id).get("coverage")
        before = old if isinstance(old, Mapping) else {}
        walk = walkthrough.as_json() if walkthrough else before.get("walkthrough")
        read = correctness.as_json() if correctness else before.get("correctness")
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
                "walkthrough": walk,
                "correctness": read,
            },
        )

    # --- bookkeeping ---------------------------------------------------------------------

    def _reserve(self, round_id: int, kinds: tuple[JobKind, ...]) -> set[JobKind]:
        added = set(kinds) - self._in_flight.get(round_id, set())
        self._in_flight.setdefault(round_id, set()).update(added)
        return added

    def _release(self, round_id: int, kinds: set[JobKind]) -> None:
        busy = self._in_flight.get(round_id)
        if busy is not None:
            busy -= kinds
            if not busy:
                del self._in_flight[round_id]

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
