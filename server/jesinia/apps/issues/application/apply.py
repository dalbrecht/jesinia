# SPDX-License-Identifier: AGPL-3.0-or-later
"""The apply engine (spec §6.1, §6.2, §8; plan Task 6; rulings IR4, IR5, IR12–IR14).

``ApplyEngine.apply`` runs a ``ChangeBatch`` as its ordered series (``domain.changes.series``):
the repo-level ``EnsureLabel`` and ``CreateIssue`` steps first, then each issue's labels,
parent, sub-issue order, close and comment steps. One engine serves approvals (which apply at
once) and the pending-changes tray.

- **Fresh reads:** each issue is read from GitHub before its first step, and its labels are
  applied as an add/remove against that read, so labels someone changed on GitHub since the
  last sync survive, and only the labels the author touched move (Review Focus 1).
- **No implicit labels (IR4):** the repo's labels are read fresh once per batch, and every
  label about to be added (to an issue, or to an issue being created) is read again live right
  before the write. A missing one fails the step and nothing is added or created. A label
  ``remove_label`` can't find is a no-op (the client tells "label gone" from "issue gone").
- **EnsureLabel (IR12)** runs through Task 4's ``LabelExecutor``; then its approved schema
  entry is recorded in the overlay. When it fails, the issues needing that label are skipped
  before their labels step, with its cause.
- **Failures:** an issue's series stops at its first failure; the rest of that issue is
  skipped; other issues carry on. A ``CreateIssue`` that fails skips every step naming its temp
  id. ``retry`` re-runs only the steps that didn't apply, with the issues already created.
- **Rate limits (IR5):** a rate limit or a rejected token stops the whole batch: the step at
  hand and every step left are failed with the cause (and GitHub's reset time). Writes are
  paced ``WRITE_INTERVAL`` apart, like the label batches.
- **Error isolation:** after a batch the cache is updated (labels, the touched issues, the
  overlay's recompute) best effort. A failure there is logged with its cause and never fails the
  writes already made.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from typing import cast

from jesinia.apps.issues.application.labels import (
    WRITE_INTERVAL,
    Clock,
    LabelExecutor,
    OnRateLimit,
    Pause,
)
from jesinia.apps.issues.application.ports import ApplyCache, IssueWrites
from jesinia.apps.issues.application.sync import OnTriaged
from jesinia.apps.issues.domain.changes import (
    ALREADY_CLOSED,
    IN_FLIGHT,
    NOT_RUN,
    UNREPEATABLE,
    UNSURE,
    BatchResult,
    ChangeBatch,
    ChangeDomainError,
    ChangedOnGitHubError,
    Close,
    CreatedIssue,
    CreateIssue,
    EnsureLabel,
    Existing,
    LabelChange,
    MissingLabelError,
    New,
    ReorderChild,
    SetParent,
    Step,
    StepResult,
    Target,
    comment_body,
    label_edit,
    labels_needed,
    series,
)
from jesinia.apps.issues.domain.labels import CreateLabel
from jesinia.apps.issues.domain.schema import LabelSchema, label_key
from jesinia.apps.issues.domain.triage import RepoNotWatchedError, Triage, TriageDomainError, triage
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.issue import (
    Issue,
    IssueDomainError,
    IssueRef,
    Label,
    validate_comment_body,
)

log = logging.getLogger(__name__)

# Told the repo whose schema overlay an apply changed (``SyncService.recompute``).
OnSchemaChanged = Callable[[str], Awaitable[None]]
# Told a batch's result before the cache is updated.
OnResult = Callable[[BatchResult], None]
_ISSUE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/issues/\d+$")
_STEP_ERRORS = (GitHubError, IssueDomainError, ChangeDomainError, TriageDomainError)


class _StepFailedError(Exception):
    """A step that can't run (internal), with the cause to report."""


class _StepSkippedError(Exception):
    """A step with nothing it can do now (internal), with the cause to report; unlike a
    failure, the issue's later steps still run."""


class _BatchStoppedError(Exception):
    """GitHub stopped the batch (internal): a rate limit or a rejected token."""

    def __init__(self, cause: str, until: datetime | None, is_rate_limited: bool) -> None:
        super().__init__(cause)
        self.until = until
        self.is_rate_limited = is_rate_limited

    @classmethod
    def of(cls, exc: GitHubError) -> _BatchStoppedError:
        limited = isinstance(exc, GitHubRateLimitedError)
        until = exc.reset_at if isinstance(exc, GitHubRateLimitedError) else None
        return cls(str(exc), until, limited)


def _repo_of(issue: Issue) -> str:
    """The repo an issue lives in, from its URL (a parent read through a child's path carries
    the child's repo name)."""
    match = _ISSUE_URL.match(issue.url)
    return match.group(1) if match else issue.repo


@dataclass
class _Run:
    """One run of a batch: what it learnt along the way."""

    batch: ChangeBatch
    created: dict[str, CreatedIssue]
    labels: dict[str, list[Label]]
    fresh: dict[tuple[str, int], Issue]
    ensure_failed: dict[tuple[str, str], str]
    create_failed: dict[str, str]
    issue_failed: set[tuple[str, int]]
    overlay_repos: set[str]
    ensured: dict[str, str]
    closed_before: set[tuple[str, int]] = field(default_factory=set)
    has_written: bool = False


class ApplyEngine:
    """Applies change batches to GitHub (see the module docstring).

    Args:
        github: issue reads and writes (``IssueClient``).
        cache: the issue cache (``SqliteIssuesStore``).
        clock: the current UTC time.
        pause: awaited ``WRITE_INTERVAL`` between writes (``asyncio.sleep``).
        on_rate_limit: told GitHub's reset time when a rate limit stops a batch.
        on_schema_changed: awaited with each repo whose overlay changed (best effort).
        on_triaged: told the touched issues' triage after a batch (best effort).
        blocked_until: GitHub's reset time while a rate limit is known (``SyncService``'s
            back-off); a batch started before it asks GitHub nothing and stops at once.
    """

    def __init__(
        self,
        github: IssueWrites,
        cache: ApplyCache,
        clock: Clock,
        *,
        pause: Pause = asyncio.sleep,
        on_rate_limit: OnRateLimit | None = None,
        on_schema_changed: OnSchemaChanged | None = None,
        on_triaged: OnTriaged | None = None,
        blocked_until: Callable[[], datetime | None] | None = None,
    ) -> None:
        self._github = github
        self._cache = cache
        self._clock = clock
        self._pause = pause
        self._executor = LabelExecutor(github, pause=pause)
        self._on_rate_limit = on_rate_limit
        self._on_schema_changed = on_schema_changed
        self._on_triaged = on_triaged
        self._blocked_until = blocked_until or (lambda: None)
        self._lock = asyncio.Lock()

    async def apply(
        self,
        batch: ChangeBatch,
        *,
        on_result: OnResult | None = None,
        on_step: OnResult | None = None,
    ) -> BatchResult:
        """Run every step of ``batch``; never raises for GitHub's answers (they are results).
        ``on_step`` is told the result so far after every step (steps still to run are ``not
        run yet``), so a write-ahead record knows what went out. ``on_result`` is called with
        the result before the cache is updated (an approval settles its suggestion there, so
        the update's draft retiring sees it approved)."""
        return await self._run(batch, None, on_result, on_step)

    async def retry(
        self,
        batch: ChangeBatch,
        previous: BatchResult,
        *,
        on_result: OnResult | None = None,
        on_step: OnResult | None = None,
    ) -> BatchResult:
        """Run again only the steps of ``previous`` that didn't apply (failed or skipped),
        with the issues it already created."""
        return await self._run(batch, previous, on_result, on_step)

    # The run ----------------------------------------------------------------------------

    async def _run(
        self,
        batch: ChangeBatch,
        previous: BatchResult | None,
        on_result: OnResult | None,
        on_step: OnResult | None,
    ) -> BatchResult:
        async with self._lock:
            steps = series(batch)
            run = _Run(
                batch,
                dict(previous.created) if previous else {},
                {},
                {},
                {},
                {},
                set(),
                set(),
                {},
            )
            kept = previous.results if previous else ()
            # A close the first run found already closed withheld its comment: so does Retry.
            run.closed_before.update(
                cast(Target, r.step.target).key
                for r in kept
                if r.step.kind == "close" and r.state == "done" and r.cause == ALREADY_CLOSED
            )
            results: list[StepResult] = []
            stopped: _BatchStoppedError | None = None
            until = self._blocked_until()
            if until is not None and self._clock() < until:
                stopped = _BatchStoppedError(
                    f"GitHub is rate limiting until {until.isoformat()}", until, True
                )
            for index, step in enumerate(steps):
                if index < len(kept) and kept[index].state == "done":
                    results.append(kept[index])
                elif stopped is not None:
                    results.append(StepResult(step, "failed", self._not_run(stopped)))
                elif (
                    index < len(kept)
                    and kept[index].cause == IN_FLIGHT
                    and step.kind in UNREPEATABLE
                ):
                    results.append(self._failed(run, step, UNSURE))
                else:
                    if step.kind in UNREPEATABLE:
                        self._progress(on_step, steps, kept, results, run, attempting=step)
                    try:
                        results.append(await self._step(run, step))
                    except _BatchStoppedError as exc:
                        stopped = exc
                        results.append(StepResult(step, "failed", str(exc)))
                    self._progress(on_step, steps, kept, results, run)
            result = BatchResult(
                tuple(results),
                run.created,
                stopped=None if stopped is None else str(stopped),
                is_rate_limited=stopped is not None and stopped.is_rate_limited,
                rate_limited_until=None if stopped is None else stopped.until,
            )
            if result.is_rate_limited and self._on_rate_limit is not None:
                self._on_rate_limit(result.rate_limited_until)
            self._log(result)
            if on_result is not None:
                on_result(result)
            await self._update_cache(run)
            return result

    @staticmethod
    def _progress(
        on_step: OnResult | None,
        steps: Sequence[Step],
        kept: Sequence[StepResult],
        results: Sequence[StepResult],
        run: _Run,
        *,
        attempting: Step | None = None,
    ) -> None:
        """Tell ``on_step`` the result so far; best effort (the write already happened). With
        ``attempting``, that step is recorded ``in flight`` first: a stop or crash during its
        write leaves it unsure, never "not run"."""
        if on_step is None:
            return
        current = [] if attempting is None else [StepResult(attempting, "skipped", IN_FLIGHT)]
        rest = [
            kept[i]
            if i < len(kept) and kept[i].state == "done"
            else StepResult(s, "skipped", NOT_RUN)
            for i, s in enumerate(steps)
        ][len(results) + len(current) :]
        try:
            on_step(BatchResult((*results, *current, *rest), dict(run.created)))
        except Exception:
            log.exception("issues.apply: recording a step's progress failed")

    @staticmethod
    def _not_run(stopped: _BatchStoppedError) -> str:
        reason = "rate limit" if stopped.is_rate_limited else "unavailability"
        return f"not run: the batch stopped at GitHub's {reason} ({stopped})"

    async def _step(self, run: _Run, step: Step) -> StepResult:
        target = step.target
        is_issue_step = step.kind not in ("ensure_label", "create_issue")
        try:
            if is_issue_step and target is not None:
                skipped = self._skip_cause(run, step, target)
                if skipped is not None:
                    run.issue_failed.add(target.key)
                    return StepResult(step, "skipped", skipped)
            cause = await self._do(run, step)
        except _StepSkippedError as exc:
            return StepResult(step, "skipped", str(exc))
        except ChangedOnGitHubError as exc:
            return replace(self._failed(run, step, str(exc)), is_conflict=True)
        except _StepFailedError as exc:
            return self._failed(run, step, str(exc))
        except _STEP_ERRORS as exc:
            return self._failed(run, step, str(exc))
        except _BatchStoppedError:
            raise
        except Exception as exc:  # a bug or a local failure: this step fails, the batch goes on
            log.exception("issues.apply: a %s step failed unexpectedly", step.kind)
            return self._failed(run, step, f"unexpected {type(exc).__name__} (see the server log)")
        return StepResult(step, "done", cause)

    @staticmethod
    def _failed(run: _Run, step: Step, cause: str) -> StepResult:
        change = step.changes[0]
        if isinstance(change, EnsureLabel):
            run.ensure_failed[(change.repo.casefold(), label_key(change.name))] = cause
        elif isinstance(change, CreateIssue):
            run.create_failed[change.temp_id] = cause
        else:
            run.issue_failed.add(cast(Target, step.target).key)
        return StepResult(step, "failed", cause)

    def _skip_cause(self, run: _Run, step: Step, target: Target) -> str | None:
        """Why an issue's step can't run now, or ``None``."""
        if target.key in run.issue_failed:
            return "not run: an earlier step of this issue didn't apply"
        refs: list[Target] = [target]
        for change in step.changes:
            if isinstance(change, SetParent) and change.parent is not None:
                refs.append(change.parent)
            elif isinstance(change, ReorderChild):
                refs += [change.child] + ([] if change.after is None else [change.after])
        for ref in refs:
            if isinstance(ref, New) and ref.temp_id not in run.created:
                cause = run.create_failed.get(ref.temp_id, "it wasn't created")
                return f"not run: {ref} wasn't created: {cause}"
        if step.kind == "labels":
            return self._ensure_cause(run, step)
        return None

    def _ensure_cause(self, run: _Run, step: Step) -> str | None:
        failed = {
            name: cause
            for (repo, name), cause in run.ensure_failed.items()
            if repo == step.repo.casefold()
        }
        if not failed:
            return None
        schema = self._schema(step.repo)
        for name in labels_needed(step.changes, schema):
            if name in failed:
                return (
                    f"not run: it needs the label {name!r}, which couldn't be created: "
                    f"{failed[name]}"
                )
        return None

    async def _do(self, run: _Run, step: Step) -> str | None:
        handlers = {
            "ensure_label": self._ensure,
            "create_issue": self._create,
            "labels": self._labels,
            "parent": self._parent,
            "order": self._order,
            "close": self._close,
            "comment": self._comment,
        }
        return await handlers[step.kind](run, step)

    # GitHub calls -----------------------------------------------------------------------

    async def _read[T](self, call: Callable[[], Awaitable[T]]) -> T:
        try:
            return await call()
        except (GitHubRateLimitedError, GitHubUnavailable) as exc:
            raise _BatchStoppedError.of(exc) from exc

    async def _write[T](self, run: _Run, call: Callable[[], Awaitable[T]]) -> T:
        if run.has_written:
            await self._pause(WRITE_INTERVAL.total_seconds())
        run.has_written = True
        return await self._read(call)

    async def _repo_labels(self, run: _Run, repo: str) -> list[Label]:
        key = repo.casefold()
        if key not in run.labels:
            run.labels[key] = await self._read(lambda: self._github.labels(repo))
        return run.labels[key]

    async def _confirm_live(self, run: _Run, repo: str, names: Sequence[str]) -> None:
        """Read each label live right before it is added: GitHub would create a missing one."""
        for name in names:
            if await self._read(partial(self._github.label, repo, name)) is None:
                run.labels[repo.casefold()] = [
                    lab
                    for lab in run.labels.get(repo.casefold(), [])
                    if label_key(lab.name) != label_key(name)
                ]
                raise MissingLabelError([name])

    def _schema(self, repo: str) -> LabelSchema:
        state = self._cache.repo(repo)
        if state is None:
            raise RepoNotWatchedError(f"{repo} is not a watched repo; add it first")
        return state.schema

    def _where(self, run: _Run, target: Target) -> tuple[str, int]:
        if isinstance(target, Existing):
            return target.repo, target.n
        created = run.created[target.temp_id]
        return created.repo, created.n

    async def _fresh(self, run: _Run, target: Target) -> Issue:
        """The issue as GitHub has it now (read once per run, then kept up to date)."""
        if target.key not in run.fresh:
            repo, n = self._where(run, target)
            run.fresh[target.key] = await self._read(lambda: self._github.get(repo, n))
        return run.fresh[target.key]

    async def _id(self, run: _Run, target: Target) -> int:
        if isinstance(target, New):
            return run.created[target.temp_id].id
        return (await self._fresh(run, target)).id

    # Steps ------------------------------------------------------------------------------

    async def _ensure(self, run: _Run, step: Step) -> str | None:
        change = cast(EnsureLabel, step.changes[0])
        labels = await self._repo_labels(run, change.repo)
        if run.has_written:
            await self._pause(WRITE_INTERVAL.total_seconds())
        op = CreateLabel(change.name, change.color, change.description)
        outcome = await self._executor.run(change.repo, [op], labels)
        if outcome.stopped is not None:
            raise _BatchStoppedError(
                outcome.stopped, outcome.rate_limited_until, outcome.is_rate_limited
            )
        (result,) = outcome.results
        if result.state != "done":
            raise _StepFailedError(result.cause or "the label couldn't be created")
        if result.cause is None:
            run.has_written = True
        run.labels[change.repo.casefold()] = list(outcome.labels)
        run.ensured[change.repo.casefold()] = change.repo
        if change.schema_entry is not None:
            try:
                self._cache.add_overlay(change.repo, change.schema_entry, self._clock())
            except Exception as exc:  # the label exists; the approval's schema entry didn't land
                log.exception("issues.apply: recording %s in the schema overlay", change.name)
                raise _StepFailedError(
                    f"{change.name!r} exists on GitHub, but recording it in the schema failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            run.overlay_repos.add(change.repo)
        return result.cause

    async def _create(self, run: _Run, step: Step) -> str | None:
        change = cast(CreateIssue, step.changes[0])
        failed = [
            cause
            for (repo, name), cause in run.ensure_failed.items()
            if repo == change.repo.casefold() and name in {label_key(n) for n in change.labels}
        ]
        if failed:
            raise _StepFailedError(f"a label it needs couldn't be created: {failed[0]}")
        known = {label_key(lab.name): lab.name for lab in await self._repo_labels(run, change.repo)}
        missing = [name for name in change.labels if label_key(name) not in known]
        if missing:
            raise MissingLabelError(missing)
        names = [known[label_key(name)] for name in change.labels]
        await self._confirm_live(run, change.repo, names)
        created = await self._write(
            run, lambda: self._github.create_issue(change.repo, change.title, change.body, names)
        )
        run.created[change.temp_id] = CreatedIssue(_repo_of(created), created.number, created.id)
        run.fresh[New(change.temp_id).key] = created
        return None

    async def _labels(self, run: _Run, step: Step) -> str | None:
        target = cast(Target, step.target)  # an issue step always has its issue
        issue = await self._fresh(run, target)
        repo, n = self._where(run, target)
        labels = await self._repo_labels(run, repo)
        edit = label_edit(
            cast(tuple[LabelChange, ...], step.changes),
            issue.labels,
            [lab.name for lab in labels],
            self._schema(repo),
        )
        if edit.is_empty:
            return "already so on GitHub"
        await self._confirm_live(run, repo, edit.add)
        if edit.add:
            now = await self._write(run, lambda: self._github.add_labels(repo, n, edit.add))
            run.fresh[target.key] = replace(run.fresh[target.key], labels=tuple(now))
        for name in edit.remove:
            now = await self._write(run, partial(self._github.remove_label, repo, n, name))
            run.fresh[target.key] = replace(run.fresh[target.key], labels=tuple(now))
        return None

    async def _parent(self, run: _Run, step: Step) -> str | None:
        target = cast(Target, step.target)  # an issue step always has its issue
        change = cast(SetParent, step.changes[-1])
        issue = await self._fresh(run, target)
        repo, n = self._where(run, target)
        current = await self._read(lambda: self._github.parent(repo, n))
        if change.expects_parent:
            self._check_parent(change.expected_parent, current)
        if change.parent is None:
            if current is None:
                return "already has no parent"
            await self._write(
                run,
                lambda: self._github.remove_sub_issue(_repo_of(current), current.number, issue.id),
            )
            run.fresh[target.key] = replace(issue, parent=None)
            return None
        wanted = await self._id(run, change.parent)
        if current is not None and current.id == wanted:
            return "already the parent"
        parent_repo, parent_n = self._where(run, change.parent)
        await self._write(
            run,
            lambda: self._github.add_sub_issue(
                parent_repo, parent_n, issue.id, replace_parent=current is not None
            ),
        )
        run.fresh[target.key] = replace(issue, parent=IssueRef(parent_repo, parent_n, wanted))
        if isinstance(change.parent, Existing) and change.parent.key in run.fresh:
            epic = run.fresh[change.parent.key]  # linking may make it an epic
            run.fresh[change.parent.key] = replace(epic, sub_issue_count=epic.sub_issue_count + 1)
        return None

    @staticmethod
    def _check_parent(expected: Existing | None, current: Issue | None) -> None:
        """Raises: ChangedOnGitHubError: GitHub's parent isn't the one the change expected."""
        now = None if current is None else (_repo_of(current).casefold(), current.number)
        if now == (None if expected is None else expected.key):
            return
        shown = "no parent" if current is None else f"{_repo_of(current)}#{current.number}"
        before = "no parent" if expected is None else str(expected)
        raise ChangedOnGitHubError(
            f"moved on GitHub since it was staged: it is under {shown} now, not {before}; "
            "nothing was changed"
        )

    async def _order(self, run: _Run, step: Step) -> str | None:
        target = cast(Target, step.target)  # an issue step always has its issue
        repo, n = self._where(run, target)
        epic = await self._id(run, target)
        changes = cast(tuple[ReorderChild, ...], step.changes)
        for change in changes:  # every child first: GitHub refuses one that left (a 422)
            child_repo, child_n = self._where(run, change.child)
            parent = await self._read(partial(self._github.parent, child_repo, child_n))
            if parent is None or parent.id != epic:
                raise _StepSkippedError(
                    f"not run: {child_repo}#{child_n} isn't a sub-issue of {repo}#{n} on GitHub "
                    "any more, so there is nothing to reorder"
                )
        for change in changes:
            child = await self._id(run, change.child)
            after = None if change.after is None else await self._id(run, change.after)
            await self._write(
                run, partial(self._github.reprioritize_sub_issue, repo, n, child, after)
            )
        return None

    async def _close(self, run: _Run, step: Step) -> str | None:
        target = cast(Target, step.target)  # an issue step always has its issue
        change = cast(Close, step.changes[-1])
        issue = await self._fresh(run, target)
        if issue.state == "closed":
            if change.only_if_open:
                raise ChangedOnGitHubError(
                    "changed on GitHub since the last sync: it was closed already "
                    f"({issue.state_reason or 'no reason'}), so it isn't closed again"
                )
            run.closed_before.add(target.key)
            return ALREADY_CLOSED
        repo, n = self._where(run, target)
        closed = await self._write(
            run, lambda: self._github.close(repo, n, change.reason, change.duplicate_of_id)
        )
        run.fresh[target.key] = closed
        return None

    async def _comment(self, run: _Run, step: Step) -> str | None:
        target = cast(Target, step.target)  # an issue step always has its issue
        await self._fresh(run, target)
        text = comment_body(step, with_close=target.key not in run.closed_before)
        if not text:
            return "nothing to post: it was already closed"
        body = validate_comment_body(text)
        repo, n = self._where(run, target)
        await self._write(run, lambda: self._github.comment(repo, n, body))
        return None

    # After the run (best effort) --------------------------------------------------------

    async def _update_cache(self, run: _Run) -> None:
        """Bring the cache up to date with what the run did; every failure is logged with its
        cause and never fails the batch (the next sync corrects the cache)."""
        for repo in sorted(run.overlay_repos):
            await self._best_effort(f"recomputing {repo}'s triage", self._schema_changed(repo))
        for key, repo in run.ensured.items():
            labels = run.labels[key]
            self._guard(
                f"caching {repo}'s labels", lambda: self._cache.replace_labels(repo, labels)
            )
        by_repo: dict[str, list[tuple[Issue, Triage]]] = {}
        for issue in run.fresh.values():

            def refresh(issue: Issue = issue) -> None:
                result = triage(issue.labels, issue.sub_issue_count, self._schema(issue.repo))
                self._cache.refresh_issue(issue, result)
                by_repo.setdefault(issue.repo, []).append((issue, result))

            self._guard(f"caching {issue.repo}#{issue.number}", refresh)
        listener = self._on_triaged
        if listener is not None:
            for repo, triaged in by_repo.items():
                self._guard(f"retiring {repo}'s drafts", lambda: listener(repo, triaged))

    async def _schema_changed(self, repo: str) -> None:
        if self._on_schema_changed is not None:
            await self._on_schema_changed(repo)

    @staticmethod
    async def _best_effort(what: str, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:  # error-isolated: the GitHub writes already happened
            log.exception("issues.apply: %s after an apply failed", what)

    @staticmethod
    def _guard(what: str, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception:  # error-isolated: the GitHub writes already happened
            log.exception("issues.apply: %s after an apply failed", what)

    @staticmethod
    def _log(result: BatchResult) -> None:
        counts = {
            state: sum(r.state == state for r in result.results)
            for state in ("done", "failed", "skipped")
        }
        log.info(
            "issues.apply batch done=%d failed=%d skipped=%d stopped=%s rate_limited=%s",
            counts["done"],
            counts["failed"],
            counts["skipped"],
            result.stopped,
            result.is_rate_limited,
        )
