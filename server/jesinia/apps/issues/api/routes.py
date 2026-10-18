# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issues routes under ``/api/issues``: watched repos and their Settings, the Inbox and
Backlog, one issue, the label setup and migration batches, suggestion drafts (list, edit,
dismiss, approve, and runs on demand), and the pending-changes tray with its applied batches.

Every route needs the owner: the app mounts module routers behind ``require_owner``, so agent
session tokens are refused (IR2); the routes that change GitHub (applying a label batch,
approving a suggestion, applying or retrying a batch) also declare it themselves. Every write
takes an ``Idempotency-Key``. GET routes serve ``issues.sqlite`` only and never await GitHub
(spec §6.3). A preview is a POST only for its body and isn't keyed: it reads
GitHub and keeps a plan, and repeating it is harmless. Typed errors become ``{"error",
"detail"}`` JSON with their status; a GitHub rate limit is a 503 with ``Retry-After`` (so the
response is never stored under the key) wherever it happens.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from jesinia.apps.issues.api.schemas import (
    REPO_MAX,
    ApplyBatchOut,
    IssueDetailOut,
    IssuePriorityRunOut,
    IssuesErrorOut,
    IssuesRepoIn,
    IssuesRepoOut,
    IssuesReposOut,
    IssueSuggestionEditIn,
    IssueSuggestionOut,
    IssueSuggestionRunIn,
    IssueSuggestionRunOut,
    IssueSuggestionRunsOut,
    IssueSuggestionsOut,
    IssueViewOut,
    LabelApplyIn,
    LabelApplyOut,
    LabelPlanIn,
    LabelPlanOut,
    PendingApplyIn,
    PendingIn,
    PendingListOut,
    PendingOut,
    SuggestionApproveOut,
    body_change,
)
from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.pending import ApplyService, PendingService
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.suggest import SuggestService
from jesinia.apps.issues.application.sync import SYNC_INTERVAL, SyncService
from jesinia.apps.issues.domain.changes import (
    BatchNotFoundError,
    BatchRunningError,
    ChangeDomainError,
    NothingPendingError,
    PendingNotFoundError,
)
from jesinia.apps.issues.domain.labels import (
    LabelNameCollisionError,
    LabelPlanError,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanNotFoundError,
    LabelPlanOutdatedError,
)
from jesinia.apps.issues.domain.schema import InvalidSettingsSchemaError, validate_settings_schema
from jesinia.apps.issues.domain.suggestions import (
    OPEN_STATES,
    DuplicateNotCachedError,
    SuggestionDomainError,
    SuggestionFilters,
    SuggestionNotFoundError,
    SuggestionRunError,
    SuggestionRunnerUnavailableError,
    SuggestionStaleError,
    SuggestionStateError,
    parse_kind,
    parse_state,
)
from jesinia.apps.issues.domain.triage import (
    IssueFilters,
    IssueNotCachedError,
    RepoNotWatchedError,
    TriageDomainError,
    parse_saved_filter,
    parse_view,
)
from jesinia.core.auth import Principal, require_owner
from jesinia.core.idempotency import IdempotencyStore, IdempotentCall, idempotent
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.issue import IssueDomainError
from jesinia.services.github.domain.repo import GitHubReferenceError


class IssuesHost(Protocol):
    """What the routes need from the module (each property answers 503 until it can)."""

    @property
    def sync(self) -> SyncService: ...

    @property
    def labels(self) -> LabelSetupService: ...

    @property
    def queries(self) -> IssueQueries: ...

    @property
    def suggest(self) -> SuggestService: ...

    @property
    def pending(self) -> PendingService: ...

    @property
    def apply(self) -> ApplyService: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...

    def clock(self) -> datetime:
        """The current UTC time."""


_STATUS: dict[type[Exception], int] = {
    RepoNotWatchedError: status.HTTP_404_NOT_FOUND,
    IssueNotCachedError: status.HTTP_404_NOT_FOUND,
    LabelPlanNotFoundError: status.HTTP_404_NOT_FOUND,
    LabelPlanExpiredError: status.HTTP_409_CONFLICT,
    LabelPlanKindMismatchError: status.HTTP_409_CONFLICT,
    LabelPlanOutdatedError: status.HTTP_409_CONFLICT,
    LabelNameCollisionError: status.HTTP_409_CONFLICT,
    SuggestionNotFoundError: status.HTTP_404_NOT_FOUND,
    SuggestionStateError: status.HTTP_409_CONFLICT,
    SuggestionStaleError: status.HTTP_409_CONFLICT,
    DuplicateNotCachedError: status.HTTP_409_CONFLICT,
    # The agent, not the request, failed or is away: suggestions wait, manual triage carries on.
    SuggestionRunnerUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    SuggestionRunError: status.HTTP_502_BAD_GATEWAY,
    PendingNotFoundError: status.HTTP_404_NOT_FOUND,
    NothingPendingError: status.HTTP_409_CONFLICT,
    BatchRunningError: status.HTTP_409_CONFLICT,
    BatchNotFoundError: status.HTTP_404_NOT_FOUND,
    # GitHub, not this server, failed: the cause is in ``detail``.
    GitHubUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubRateLimitedError: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubError: status.HTTP_502_BAD_GATEWAY,
    # Anything else the request itself got wrong.
    TriageDomainError: status.HTTP_400_BAD_REQUEST,
    LabelPlanError: status.HTTP_400_BAD_REQUEST,
    SuggestionDomainError: status.HTTP_400_BAD_REQUEST,
    ChangeDomainError: status.HTTP_400_BAD_REQUEST,
    InvalidSettingsSchemaError: status.HTTP_400_BAD_REQUEST,
    GitHubReferenceError: status.HTTP_400_BAD_REQUEST,
    IssueDomainError: status.HTTP_400_BAD_REQUEST,
}
_TYPED = (
    TriageDomainError,
    ChangeDomainError,
    LabelPlanError,
    SuggestionDomainError,
    SuggestionRunError,
    InvalidSettingsSchemaError,
    GitHubError,
    GitHubReferenceError,
    IssueDomainError,
)
# ``Any`` is FastAPI's own type for ``responses`` (free-form OpenAPI fragments).
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": IssuesErrorOut} for code in (400, 404, 409, 502, 503)
}

Owner = Annotated[str, Path(min_length=1, max_length=REPO_MAX)]
Name = Annotated[str, Path(min_length=1, max_length=REPO_MAX)]
Number = Annotated[int, Path(ge=1, le=999_999_999)]
SuggestionId = Annotated[str, Path(min_length=1, max_length=64)]
ItemId = Annotated[str, Path(min_length=1, max_length=64)]


def retry_after(reset_at: datetime | None, now: datetime) -> int:
    """Seconds until GitHub's reset time (at least 1), or one sync interval without one."""
    if reset_at is None:
        return int(SYNC_INTERVAL.total_seconds())
    return max(1, math.ceil((reset_at - now).total_seconds()))


def _status_of(exc: Exception) -> int:
    """The status of the most specific mapped class (every ``_TYPED`` base is mapped)."""
    return next(_STATUS[kind] for kind in type(exc).__mro__ if kind in _STATUS)


def _error(exc: Exception, now: Callable[[], datetime]) -> JSONResponse:
    wait = retry_after(exc.reset_at, now()) if isinstance(exc, GitHubRateLimitedError) else None
    body = IssuesErrorOut(error=type(exc).__name__, detail=str(exc), retry_after=wait)
    headers = None if wait is None else {"Retry-After": str(wait)}
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=_status_of(exc), headers=headers
    )


async def _respond(
    work: Callable[[], Awaitable[BaseModel | Response]], now: Callable[[], datetime]
) -> Response:
    """``work``'s model as a concrete ``Response`` (so idempotency can store it), or its typed
    error as structured JSON."""
    try:
        result = await work()
    except _TYPED as exc:
        return _error(exc, now)
    if isinstance(result, Response):
        return result
    return JSONResponse(jsonable_encoder(result))


def _repo(owner: str, repo: str) -> str:
    return f"{owner}/{repo}"


def _started(batch_id: str, body: BaseModel) -> Response:
    """A started batch's answer: 202, with the batch to poll as ``Location`` (IR45). It is
    stored under the idempotency key like any answer, and the batch keeps the key too, so a
    replay always answers this batch (IR46)."""
    return JSONResponse(
        jsonable_encoder(body),
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Location": f"/api/issues/pending/{batch_id}"},
    )


def _request_key(route: str, idem: IdempotentCall) -> str | None:
    """The key a batch keeps, by route (one key on two routes is two requests)."""
    return None if idem.key is None else f"{route}:{idem.key}"


def build_router(host: IssuesHost) -> APIRouter:
    r = APIRouter(prefix="/api/issues", tags=["issues"], responses=_ERRORS)
    keyed = Depends(idempotent(lambda: host.idempotency))
    repo_path = "/repos/{owner}/{repo}"
    item_path = "/item/{owner}/{repo}/{n}"
    labels_path = "/labels/{owner}/{repo}"

    @r.get("/repos", response_model=IssuesReposOut)
    async def get_repos() -> Response:
        """Every repo added in Settings → Issues, with its schema and sync state."""

        async def run() -> IssuesReposOut:
            return IssuesReposOut(repos=[IssuesRepoOut.of(s) for s in host.queries.repos()])

        return await _respond(run, host.clock)

    @r.put(repo_path, response_model=IssuesRepoOut)
    async def put_repo(
        owner: Owner, repo: Name, body: IssuesRepoIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Watch or unwatch a repo and keep (or clear) its Settings schema. A new repo's
        existing issues land in the Backlog on its first sync."""

        async def run() -> IssuesRepoOut:
            wanted = body.settings_schema
            schema = None if wanted is None else validate_settings_schema(wanted.model_dump())
            state = await host.sync.configure(
                _repo(owner, repo), watched=body.watched, settings_schema=schema
            )
            return IssuesRepoOut.of(state)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{repo_path}/sync", response_model=IssuesRepoOut)
    async def sync_repo(owner: Owner, repo: Name, idem: IdempotentCall = keyed) -> Response:
        """Sync a watched repo now; a failure is recorded in ``sync_error``, not raised."""

        async def run() -> IssuesRepoOut:
            name = _repo(owner, repo)
            await host.sync.sync(name)
            return IssuesRepoOut.of(host.sync.state(name))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get(item_path, response_model=IssueDetailOut)
    async def get_item(owner: Owner, repo: Name, n: Number) -> Response:
        """One issue from the cache, with the comments and timeline last fetched."""

        async def run() -> IssueDetailOut:
            return IssueDetailOut.of(host.queries.detail(_repo(owner, repo), n))

        return await _respond(run, host.clock)

    @r.post(f"{item_path}/refresh", response_model=IssueDetailOut)
    async def refresh_item(
        owner: Owner, repo: Name, n: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Fetch the issue's comments, timeline and (for an epic) children from GitHub."""

        async def run() -> IssueDetailOut:
            name = _repo(owner, repo)
            await host.sync.refresh_details(name, n)
            return IssueDetailOut.of(host.queries.detail(name, n))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{labels_path}/preview", response_model=LabelPlanOut)
    async def preview_labels(owner: Owner, repo: Name, body: LabelPlanIn) -> Response:
        """What the setup or migration batch would do, from a fresh read of the repo's labels
        (and, for a migration, each old label's issues listed live); nothing is written to
        GitHub. Apply it with its ``plan_id``.

        Deliberately not keyed, unlike the other POSTs: it changes nothing on GitHub, and the
        plan row each call keeps is harmless (plans expire and are pruned)."""

        async def run() -> LabelPlanOut:
            return LabelPlanOut.of(await host.labels.preview(_repo(owner, repo), body.kind))

        return await _respond(run, host.clock)

    apply_responses: dict[int | str, dict[str, Any]] = {
        503: {"model": LabelApplyOut, "description": "GitHub's rate limit stopped the batch"}
    }

    @r.post(f"{labels_path}/apply", response_model=LabelApplyOut, responses=apply_responses)
    async def apply_labels(
        owner: Owner,
        repo: Name,
        body: LabelApplyIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Apply a previewed batch (an approval; owner only). Refused with a 409 when the
        repo's labels or schema changed since the preview. When GitHub stops the batch (a rate
        limit, with ``Retry-After``, or a rejected token) the answer is a 503 with the steps so
        far."""

        async def run() -> Response:
            name = _repo(owner, repo)
            result = await host.labels.apply(name, body.kind, body.plan_id)
            out = jsonable_encoder(LabelApplyOut.of(name, body.kind, result))
            if result.stopped is None:
                return JSONResponse(out)
            # A 5xx is never stored under the key either way; a rate limit says when to retry.
            headers = (
                {"Retry-After": str(retry_after(result.rate_limited_until, host.clock()))}
                if result.is_rate_limited
                else None
            )
            return JSONResponse(
                out, status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers=headers
            )

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get("/suggestions", response_model=IssueSuggestionsOut)
    async def get_suggestions(
        repo: Annotated[str | None, Query(max_length=REPO_MAX)] = None,
        n: Annotated[int | None, Query(ge=1, le=999_999_999)] = None,
        state: Annotated[list[str] | None, Query(max_length=4)] = None,
        kind: Annotated[str | None, Query(max_length=32)] = None,
        scope: Annotated[str | None, Query(min_length=1, max_length=8000)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> Response:
        """Suggestions from the cache: one issue (``repo`` and ``n``, with its latest run), one
        repo, one batch ``scope`` (with its latest priority ask), or all, oldest first and at
        most ``limit``. ``state`` may repeat (default: the
        open ones, ``draft`` and ``edited``)."""

        async def run() -> IssueSuggestionsOut:
            states = OPEN_STATES if not state else tuple(parse_state(s) for s in state)
            parsed = None if kind is None else parse_kind(kind)
            filters = SuggestionFilters(repo, n, states, parsed, scope)
            latest = None
            if repo is not None and n is not None:
                found = host.suggest.run_state(repo, n)
                latest = None if found is None else IssueSuggestionRunOut.of(found)
            asked = None if scope is None else host.suggest.priority_run(scope)
            return IssueSuggestionsOut(
                suggestions=[
                    IssueSuggestionOut.of(s) for s in host.suggest.suggestions(filters, limit)
                ],
                run=latest,
                priority_run=None if asked is None else IssuePriorityRunOut.of(asked),
            )

        return await _respond(run, host.clock)

    @r.post(
        "/suggestions/run",
        response_model=IssueSuggestionRunsOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_suggestions(body: IssueSuggestionRunIn, idem: IdempotentCall = keyed) -> Response:
        """Ask the agent for suggestions on these cached issues now; the runs start in the
        background (two at a time) and their drafts appear as each one finishes. The only way
        a Backlog issue gets suggestions."""

        async def run() -> Response:
            runs = await host.suggest.suggest_backlog([(ref.repo, ref.n) for ref in body.refs])
            out = IssueSuggestionRunsOut(runs=[IssueSuggestionRunOut.of(r) for r in runs])
            return JSONResponse(jsonable_encoder(out), status_code=status.HTTP_202_ACCEPTED)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        "/suggestions/priority",
        response_model=IssuePriorityRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def suggest_priority(
        body: IssueSuggestionRunIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Ask the agent to prioritise these cached open issues. The ask runs in the background
        (the request never waits for the agent); poll ``GET /suggestions?scope=`` with the
        answer's ``scope`` (its ``priority_run``). When it is ``done`` there is one draft
        (``kind`` ``priority``, ``scope`` the batch) with a priority, a rank and a reason per
        issue; approving it (``/suggestions/{id}/approve``) sets the priorities. Asking for a
        batch that is already being asked returns that run."""

        async def run() -> Response:
            started = await host.suggest.start_priority([(r.repo, r.n) for r in body.refs])
            out = IssuePriorityRunOut.of(started)
            return JSONResponse(jsonable_encoder(out), status_code=status.HTTP_202_ACCEPTED)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.patch("/suggestions/{suggestion_id}", response_model=IssueSuggestionOut)
    async def edit_suggestion(
        suggestion_id: SuggestionId, body: IssueSuggestionEditIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Edit a draft (it becomes ``edited``); the payload is checked against the repo's
        schema. Nothing changes on GitHub. An approved or dismissed one is a 409."""

        async def run() -> IssueSuggestionOut:
            edited = host.suggest.edit(suggestion_id, body.payload, body.reason)
            return IssueSuggestionOut.of(edited)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post("/suggestions/{suggestion_id}/dismiss", response_model=IssueSuggestionOut)
    async def dismiss_suggestion(
        suggestion_id: SuggestionId, idem: IdempotentCall = keyed
    ) -> Response:
        """Dismiss a draft; a rerun won't propose the same advice again. Dismissing it twice
        changes nothing; an approved one is a 409."""

        async def run() -> IssueSuggestionOut:
            return IssueSuggestionOut.of(host.suggest.dismiss(suggestion_id))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        "/suggestions/{suggestion_id}/approve",
        response_model=SuggestionApproveOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def approve_suggestion(
        suggestion_id: SuggestionId,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Approve a suggestion: it applies to GitHub at once, as one ordered series, in the
        background. The answer is a 202 with the batch (``running``) and ``Location`` to poll;
        the suggestion is ``approved`` once its batch completes, or stays approvable with
        ``apply_error`` (a conflict with GitHub's fresh read included). Refused with a 409 when
        its premise is gone in the cache (nothing applied), when it was approved or dismissed
        or is being approved, or for a duplicate whose original isn't cached."""

        async def run() -> Response:
            approval = host.apply.start_approval(suggestion_id, _request_key("approve", idem))
            out = SuggestionApproveOut(
                suggestion=IssueSuggestionOut.of(approval.suggestion),
                batch=ApplyBatchOut.of(approval.batch),
            )
            return _started(approval.batch.id, out)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get("/pending", response_model=PendingListOut)
    async def get_pending() -> Response:
        """The pending-changes tray, in the order staged (no GitHub)."""

        async def run() -> PendingListOut:
            return PendingListOut(changes=[PendingOut.of(p) for p in host.pending.pending()])

        return await _respond(run, host.clock)

    @r.post("/pending", response_model=PendingOut, status_code=status.HTTP_201_CREATED)
    async def stage_pending(body: PendingIn, idem: IdempotentCall = keyed) -> Response:
        """Stage a change for a cached issue (a drag, a picker, a menu, the bulk bar or a queue
        move); nothing reaches GitHub until Apply."""

        async def run() -> Response:
            change = body_change(body.change, host.pending.original)
            staged = host.pending.stage(body.repo, body.n, change, body.source)
            return JSONResponse(
                jsonable_encoder(PendingOut.of(staged)), status_code=status.HTTP_201_CREATED
            )

        return await idem.run(lambda: _respond(run, host.clock))

    @r.delete("/pending/{pending_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def unstage_pending(pending_id: ItemId, idem: IdempotentCall = keyed) -> Response:
        """Undo one staged change."""

        async def run() -> Response:
            host.pending.unstage(pending_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.delete("/pending", status_code=status.HTTP_204_NO_CONTENT)
    async def clear_pending(idem: IdempotentCall = keyed) -> Response:
        """Undo every staged change."""

        async def run() -> Response:
            host.pending.clear()
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post("/pending/apply", response_model=ApplyBatchOut, status_code=status.HTTP_202_ACCEPTED)
    async def apply_pending(
        body: PendingApplyIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Apply the staged changes (or only ``ids``) as one batch: per issue, one ordered
        series against a fresh read. They leave the tray at once, into a stored batch that runs
        in the background: the answer is a 202 with it (``running``) and ``Location`` to poll.
        A replay of the same request answers the same batch and never applies the tray
        again."""

        async def run() -> Response:
            started = host.apply.start_pending(body.ids, _request_key("apply", idem))
            return _started(started.id, ApplyBatchOut.of(started))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get("/pending/{batch}", response_model=ApplyBatchOut)
    async def get_batch(batch: ItemId) -> Response:
        """A batch and its result, step by step (no GitHub): poll it while ``running``. A
        ``finished`` batch that GitHub stopped says so in ``stopped`` (and
        ``rate_limited_until``); resume it, or an ``interrupted`` one, with Retry."""

        async def run() -> ApplyBatchOut:
            return ApplyBatchOut.of(host.pending.batch(batch))

        return await _respond(run, host.clock)

    @r.post(
        "/pending/{batch}/retry",
        response_model=ApplyBatchOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_batch(
        batch: ItemId,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Run again only the steps of a batch that didn't apply (failed or skipped), in the
        background: a 202 with the batch and ``Location`` to poll. A batch still running is a
        409."""

        async def run() -> Response:
            again = host.apply.start_retry(batch)
            return _started(again.id, ApplyBatchOut.of(again))

        return await idem.run(lambda: _respond(run, host.clock))

    # Last: ``/{view}`` would otherwise also match ``/repos``.
    @r.get("/{view}", response_model=IssueViewOut)
    async def get_view(
        view: Annotated[str, Path(max_length=32)],
        repo: Annotated[str | None, Query(max_length=REPO_MAX)] = None,
        saved: Annotated[str | None, Query(alias="filter", max_length=64)] = None,
    ) -> Response:
        """The cached Inbox or Backlog, at once, even without the github service;
        ``syncing`` and ``sync_error`` report GitHub. ``filter`` is a saved filter
        (``incomplete``, ``stale_90d``, ``human_needed_could_be_ready``,
        ``possible_duplicates``)."""

        async def run() -> IssueViewOut:
            filters = IssueFilters(repo, parse_saved_filter(saved))
            return IssueViewOut.of(host.queries.view(parse_view(view), filters))

        return await _respond(run, host.clock)

    return r
