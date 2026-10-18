# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issues routes under ``/api/issues``: watched repos and their Settings, the Inbox and
Backlog, one issue, the label setup and migration batches, suggestion drafts (list, edit,
dismiss, approve, and runs on demand), the pending-changes tray with its applied batches, and
epics (their list and page, drop checks, checklist conversion, group, split and brief asks, the
approved brief), milestones, work queues (local; Done and band moves stage in the tray), and
themes (local; publishing stages its label in the tray, and an epic is one approved batch).

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
    AbandonedOut,
    AbandonIn,
    ApplyBatchesOut,
    ApplyBatchOut,
    ContainmentOut,
    ConversionIn,
    ConversionPlanOut,
    EpicBriefIn,
    EpicBriefOut,
    EpicDetailOut,
    EpicOut,
    EpicsOut,
    GroupAskIn,
    IssueAskRunOut,
    IssueDetailOut,
    IssueRefIn,
    IssuesErrorOut,
    IssuesRepoIn,
    IssuesRepoOut,
    IssuesRepoPatch,
    IssuesReposOut,
    IssuesSettingsOut,
    IssuesSettingsPatch,
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
    MilestoneDetailOut,
    MilestoneOut,
    MilestonesOut,
    PendingApplyIn,
    PendingIn,
    PendingListOut,
    PendingOut,
    SuggestionApproveOut,
    ThemeAddOut,
    ThemedIssueOut,
    ThemeEpicIn,
    ThemeEpicPlanOut,
    ThemeIssuesIn,
    ThemeOut,
    ThemePublishIn,
    ThemePublishOut,
    ThemeRemoveOut,
    ThemesOut,
    ThemeSummaryOut,
    WorkQueueActionOut,
    WorkQueueApprovalOut,
    WorkQueueBandIn,
    WorkQueueDoneIn,
    WorkQueueFillOut,
    WorkQueueFromPriorityIn,
    WorkQueueIn,
    WorkQueueItemIn,
    WorkQueueItemPatchIn,
    WorkQueueOut,
    WorkQueuePatchIn,
    WorkQueuesOut,
    WorkQueueSplitAskIn,
    WorkQueueSummaryOut,
    WorkQueueWarningOut,
    WorkQueueWarningsOut,
    body_change,
)
from jesinia.apps.issues.application.epics import BriefService, EpicService
from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.pending import ApplyService, PendingService
from jesinia.apps.issues.application.queues import QueueService
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.settings import IssuesSettingsService
from jesinia.apps.issues.application.suggest import SuggestService
from jesinia.apps.issues.application.sync import KEEP, SYNC_INTERVAL, Keep, SyncService
from jesinia.apps.issues.application.themes import ThemeService
from jesinia.apps.issues.domain.changes import (
    BatchAbandonedError,
    BatchNotFoundError,
    BatchRunningError,
    ChangeDomainError,
    EarlierBatchError,
    NothingPendingError,
    PendingHeldError,
    PendingNotFoundError,
)
from jesinia.apps.issues.domain.epics import (
    BriefEditedOnGitHubError,
    BriefRecoveryIncompleteError,
    ConversionOutdatedError,
    EpicDomainError,
    GroupEpicExistsError,
    ItemKind,
    NotAnEpicError,
    NothingToConvertError,
)
from jesinia.apps.issues.domain.labels import (
    LabelNameCollisionError,
    LabelPlanError,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanNotFoundError,
    LabelPlanOutdatedError,
)
from jesinia.apps.issues.domain.queues import (
    QueueDomainError,
    QueueItemExistsError,
    QueueItemNotFoundError,
    QueueItemStateError,
    QueueNameTakenError,
    QueueNotFoundError,
)
from jesinia.apps.issues.domain.schema import (
    InvalidSettingsSchemaError,
    LabelSchema,
    validate_settings_schema,
)
from jesinia.apps.issues.domain.suggestions import (
    OPEN_STATES,
    DuplicateNotCachedError,
    PriorityRun,
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
from jesinia.apps.issues.domain.themes import (
    NothingToPublishError,
    ThemeCatalogError,
    ThemeDomainError,
    ThemeEpicClosedError,
    ThemeNearDuplicateError,
    ThemeNotFoundError,
    ThemesUnavailableError,
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
    def epics(self) -> EpicService: ...

    @property
    def briefs(self) -> BriefService: ...

    @property
    def queues(self) -> QueueService: ...

    @property
    def themes(self) -> ThemeService: ...

    @property
    def settings(self) -> IssuesSettingsService: ...

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
    BatchAbandonedError: status.HTTP_409_CONFLICT,
    EarlierBatchError: status.HTTP_409_CONFLICT,
    PendingHeldError: status.HTTP_409_CONFLICT,
    BatchNotFoundError: status.HTTP_404_NOT_FOUND,
    NotAnEpicError: status.HTTP_404_NOT_FOUND,
    ConversionOutdatedError: status.HTTP_409_CONFLICT,
    NothingToConvertError: status.HTTP_409_CONFLICT,
    BriefEditedOnGitHubError: status.HTTP_409_CONFLICT,
    BriefRecoveryIncompleteError: status.HTTP_409_CONFLICT,
    QueueNotFoundError: status.HTTP_404_NOT_FOUND,
    QueueItemNotFoundError: status.HTTP_404_NOT_FOUND,
    QueueNameTakenError: status.HTTP_409_CONFLICT,
    QueueItemExistsError: status.HTTP_409_CONFLICT,
    QueueItemStateError: status.HTTP_409_CONFLICT,
    ThemeNotFoundError: status.HTTP_404_NOT_FOUND,
    ThemeEpicClosedError: status.HTTP_409_CONFLICT,
    GroupEpicExistsError: status.HTTP_409_CONFLICT,
    ThemeNearDuplicateError: status.HTTP_409_CONFLICT,
    NothingToPublishError: status.HTTP_409_CONFLICT,
    # The KB tag catalog, not the request, is away or failed: the cause is in ``detail``.
    ThemesUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    ThemeCatalogError: status.HTTP_503_SERVICE_UNAVAILABLE,
    # GitHub, not this server, failed: the cause is in ``detail``.
    GitHubUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubRateLimitedError: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubError: status.HTTP_502_BAD_GATEWAY,
    # Anything else the request itself got wrong.
    TriageDomainError: status.HTTP_400_BAD_REQUEST,
    LabelPlanError: status.HTTP_400_BAD_REQUEST,
    SuggestionDomainError: status.HTTP_400_BAD_REQUEST,
    ChangeDomainError: status.HTTP_400_BAD_REQUEST,
    EpicDomainError: status.HTTP_400_BAD_REQUEST,
    QueueDomainError: status.HTTP_400_BAD_REQUEST,
    ThemeDomainError: status.HTTP_400_BAD_REQUEST,
    InvalidSettingsSchemaError: status.HTTP_400_BAD_REQUEST,
    GitHubReferenceError: status.HTTP_400_BAD_REQUEST,
    IssueDomainError: status.HTTP_400_BAD_REQUEST,
}
_TYPED = (
    TriageDomainError,
    ChangeDomainError,
    EpicDomainError,
    QueueDomainError,
    ThemeDomainError,
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
QueueId = Annotated[str, Path(min_length=1, max_length=64)]
Kind = Annotated[ItemKind, Path()]
Tag = Annotated[str, Path(min_length=1, max_length=100)]


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
    batch_id = exc.batch_id if isinstance(exc, EarlierBatchError) else None
    named = exc.epic if isinstance(exc, GroupEpicExistsError | ThemeEpicClosedError) else None
    body = IssuesErrorOut(
        error=type(exc).__name__,
        detail=str(exc),
        retry_after=wait,
        batch_id=batch_id,
        epic=None if named is None else f"{named[0]}#{named[1]}",
    )
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

    @r.patch(repo_path, response_model=IssuesRepoOut)
    async def patch_repo(
        owner: Owner, repo: Name, body: IssuesRepoPatch, idem: IdempotentCall = keyed
    ) -> Response:
        """Change only the fields sent: watch or unwatch the repo, or keep, replace or clear its
        Settings schema. ``watched: true`` adds a new repo (its issues land in the Backlog)."""

        async def run() -> IssuesRepoOut:
            sent = body.model_fields_set
            wanted = body.settings_schema
            schema: LabelSchema | None | Keep = KEEP
            if "settings_schema" in sent:
                schema = None if wanted is None else validate_settings_schema(wanted.model_dump())
            state = await host.sync.update(
                _repo(owner, repo), watched=body.watched, settings_schema=schema
            )
            return IssuesRepoOut.of(state)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get("/settings", response_model=IssuesSettingsOut)
    async def get_settings() -> Response:
        """Settings → Issues' own settings: the runner suggestions ask."""

        async def run() -> IssuesSettingsOut:
            return IssuesSettingsOut(runner=host.settings.settings().runner)

        return await _respond(run, host.clock)

    @r.patch("/settings", response_model=IssuesSettingsOut)
    async def patch_settings(body: IssuesSettingsPatch, idem: IdempotentCall = keyed) -> Response:
        """Save only the fields sent (``runner: null`` follows the agents service again)."""

        async def run() -> IssuesSettingsOut:
            current = host.settings.settings()
            if "runner" in body.model_fields_set:
                current = host.settings.update(runner=body.runner)
            return IssuesSettingsOut(runner=current.runner)

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
        repo, one batch ``scope`` (with its latest background ask), or all, oldest first and at
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
            asked = None if scope is None else host.suggest.ask_run(scope)
            return IssueSuggestionsOut(
                suggestions=[
                    IssueSuggestionOut.of(s) for s in host.suggest.suggestions(filters, limit)
                ],
                run=latest,
                ask_run=None if asked is None else IssueAskRunOut.of(asked),
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
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def suggest_priority(
        body: IssueSuggestionRunIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Ask the agent to prioritise these cached open issues. The ask runs in the background
        (the request never waits for the agent); poll ``GET /suggestions?scope=`` with the
        answer's ``scope`` (its ``ask_run``). When it is ``done`` there is one draft
        (``kind`` ``priority``, ``scope`` the batch) with a priority, a rank and a reason per
        issue; approving it (``/suggestions/{id}/approve``) sets the priorities. Asking for a
        batch that is already being asked returns that run."""

        async def run() -> Response:
            started = await host.suggest.start_priority([(r.repo, r.n) for r in body.refs])
            out = IssueAskRunOut.of(started)
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
            approval = await host.apply.start_approval_live(
                suggestion_id, _request_key("approve", idem)
            )
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
        """Undo one staged change. A change a queue item's Done holds is a 409
        (``PendingHeldError``): undo that Done in its queue instead."""

        async def run() -> Response:
            host.pending.unstage(pending_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.delete("/pending", status_code=status.HTTP_204_NO_CONTENT)
    async def clear_pending(idem: IdempotentCall = keyed) -> Response:
        """Undo every staged change no queue item's Done holds (those stay: undo them in their
        queue)."""

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

    @r.get("/batches", response_model=ApplyBatchesOut)
    async def get_unfinished_batches() -> Response:
        """The batches that didn't finish (running, interrupted, or finished with steps not
        applied), newest first, from the cache: what the tray offers Retry or Abandon for after
        a reload (no GitHub)."""

        async def run() -> ApplyBatchesOut:
            unfinished = host.pending.unfinished()
            return ApplyBatchesOut(batches=[ApplyBatchOut.of(b) for b in unfinished])

        return await _respond(run, host.clock)

    @r.get("/pending/{batch}", response_model=ApplyBatchOut)
    async def get_batch(batch: ItemId) -> Response:
        """A batch and its result, step by step (no GitHub): poll it while ``running``. A
        ``finished`` batch that GitHub stopped says so in ``stopped`` (and
        ``rate_limited_until``); resume it, or an ``interrupted`` one, with Retry."""

        async def run() -> ApplyBatchOut:
            return ApplyBatchOut.of(host.pending.batch(batch))

        return await _respond(run, host.clock)

    @r.post("/pending/{batch}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
    async def dismiss_batch(batch: ItemId, idem: IdempotentCall = keyed) -> Response:
        """Take a batch the author is done with out of the tray: ``GET /batches`` never lists it
        again (unless a retry runs it). Nothing changes on GitHub. A running batch is a 409."""

        async def run() -> Response:
            host.apply.dismiss(batch)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return await idem.run(lambda: _respond(run, host.clock))

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

    epic_path = "/epics/{owner}/{repo}/{n}"

    def _ask_started(run: PriorityRun) -> Response:
        out = IssueAskRunOut.of(run)
        return JSONResponse(jsonable_encoder(out), status_code=status.HTTP_202_ACCEPTED)

    @r.get("/epics", response_model=EpicsOut)
    async def get_epics(
        repo: Annotated[str | None, Query(max_length=REPO_MAX)] = None,
    ) -> Response:
        """The open epics and initiatives of the watched repos (or one ``repo``), each with its
        progress and readiness, from the cache."""

        async def run() -> EpicsOut:
            return EpicsOut(epics=[EpicOut.of(e) for e in host.epics.epics(repo)])

        return await _respond(run, host.clock)

    @r.get(epic_path, response_model=EpicDetailOut)
    async def get_epic(owner: Owner, repo: Name, n: Number) -> Response:
        """One epic: its children in order with what blocks each, its readiness, the approved
        brief and a brief composed from the cache. 404 for an issue that isn't an epic."""

        async def run() -> EpicDetailOut:
            return EpicDetailOut.of(host.epics.epic(_repo(owner, repo), n))

        return await _respond(run, host.clock)

    @r.get(f"{epic_path}/accepts", response_model=ContainmentOut)
    async def get_accepts(
        owner: Owner,
        repo: Name,
        n: Number,
        child_repo: Annotated[str, Query(min_length=3, max_length=REPO_MAX)],
        child_n: Annotated[int, Query(ge=1, le=999_999_999)],
    ) -> Response:
        """Whether ``child_repo#child_n`` may be dropped onto this issue: refused (with the
        reason) for itself, one of its ancestors, another owner's issue or an initiative into an
        epic; warned for another repo ("won't fit one PR")."""

        async def run() -> ContainmentOut:
            found = host.epics.accepts(_repo(owner, repo), n, child_repo, child_n)
            return ContainmentOut.of(found)

        return await _respond(run, host.clock)

    @r.get(f"{epic_path}/conversion", response_model=ConversionPlanOut)
    async def get_conversion(owner: Owner, repo: Name, n: Number) -> Response:
        """What converting the issue's checklist into sub-issues would do: linked items matched,
        unlinked ones drafted as new issues (no labels), and what is skipped and why. Writes
        nothing."""

        async def run() -> ConversionPlanOut:
            name = _repo(owner, repo)
            return ConversionPlanOut.of(name, n, host.epics.conversion(name, n))

        return await _respond(run, host.clock)

    @r.post(
        f"{epic_path}/conversion",
        response_model=ApplyBatchOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def convert(
        owner: Owner,
        repo: Name,
        n: Number,
        body: ConversionIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Approve the conversion previewed with ``digest``: one batch (create the new issues,
        then link every child in order) applied in the background, a 202 with the batch and
        ``Location`` to poll. A 409 when the plan changed since (preview again) or has nothing
        to do, or while another conversion of this epic is still running. The checklist stays in the
        body, and the items converted are recorded, so converting it again never creates them
        twice."""

        async def run() -> Response:
            name = _repo(owner, repo)
            started = host.epics.start_conversion(
                name, n, body.digest, _request_key("convert", idem), host.apply
            )
            return _started(started.id, ApplyBatchOut.of(started))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        f"{epic_path}/brief/draft",
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def draft_brief(
        owner: Owner, repo: Name, n: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Ask the agent for the epic's delivery brief, in the background; poll
        ``GET /suggestions?scope=`` with the answer's ``scope``. The result is a ``brief`` draft
        (``payload.text``) to edit and approve."""

        async def run() -> Response:
            return _ask_started(host.epics.start_brief(_repo(owner, repo), n))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{epic_path}/brief", response_model=EpicBriefOut)
    async def approve_brief(
        owner: Owner,
        repo: Name,
        n: Number,
        body: EpicBriefIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Approve the brief: it is posted on the epic as the ``<!-- jesinia:brief -->``
        comment, or the comment already stored is edited in place (a deleted one is posted
        anew). A comment edited on GitHub since is a 409 unless ``overwrite``. A post cut off
        by a stop is found again, never posted twice."""

        async def run() -> EpicBriefOut:
            posted = await host.briefs.approve(
                _repo(owner, repo), n, body.text, body.suggestion_id, overwrite=body.overwrite
            )
            return EpicBriefOut.of(posted)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        "/epics/proposals",
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def propose_groups(body: GroupAskIn, idem: IdempotentCall = keyed) -> Response:
        """Ask the agent to group open, ungrouped issues into candidate epics and to add some to
        existing epics, in the background; the ``group`` drafts carry the answer's ``scope``.
        Approving one creates the epic (or links the issue) through the apply engine."""

        async def run() -> Response:
            return _ask_started(host.epics.start_groups(body.repo))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        "/suggestions/split",
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def propose_split(body: IssueRefIn, idem: IdempotentCall = keyed) -> Response:
        """Ask the agent how to split an open issue too big for one PR, in the background; the
        ``split`` draft carries the answer's ``scope``. Approving it creates the parts as the
        issue's sub-issues."""

        async def run() -> Response:
            return _ask_started(host.epics.start_split(body.repo, body.n))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get("/milestones", response_model=MilestonesOut)
    async def get_milestones(
        repo: Annotated[str, Query(min_length=3, max_length=REPO_MAX)],
    ) -> Response:
        """A repo's milestones from the cache, each standing for its open issues with their
        readiness rolled up (a queue item and a filter; Jesinia never writes milestones)."""

        async def run() -> MilestonesOut:
            return MilestonesOut(
                milestones=[MilestoneOut.of(m) for m in host.epics.milestones(repo)]
            )

        return await _respond(run, host.clock)

    @r.get("/milestones/{owner}/{repo}/{n}", response_model=MilestoneDetailOut)
    async def get_milestone(owner: Owner, repo: Name, n: Number) -> Response:
        """One milestone (``n`` is its number): its open issues, their rolled-up readiness and
        the brief built from them."""

        async def run() -> MilestoneDetailOut:
            return MilestoneDetailOut.of(host.epics.milestone(_repo(owner, repo), n))

        return await _respond(run, host.clock)

    # Work queues (Task 9): local; Done and band moves stage in the tray -------------------

    queue_path = "/queues/{queue_id}"
    queue_item_path = queue_path + "/items/{kind}/{owner}/{repo}/{n}"

    @r.get("/queues", response_model=WorkQueuesOut)
    async def get_queues() -> Response:
        """Every work queue, in the author's order, with its open, done and warned counts."""

        async def run() -> WorkQueuesOut:
            return WorkQueuesOut(queues=[WorkQueueSummaryOut.of(q) for q in host.queues.queues()])

        return await _respond(run, host.clock)

    @r.post("/queues", response_model=WorkQueueOut, status_code=status.HTTP_201_CREATED)
    async def create_queue(body: WorkQueueIn, idem: IdempotentCall = keyed) -> Response:
        """A new, empty queue, last in the list. A name another queue has (without case) is a
        409."""

        async def run() -> Response:
            made = host.queues.create(body.name, body.description)
            return JSONResponse(
                jsonable_encoder(WorkQueueOut.of(made)), status_code=status.HTTP_201_CREATED
            )

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post("/queues/from-priority", response_model=WorkQueueFillOut)
    async def queue_from_priority(
        body: WorkQueueFromPriorityIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Turn a priority draft's order into a new queue (``name``) or append it to one
        (``queue_id``): its open cached issues in rank order; ``skipped`` says what wasn't
        added and why. Local only; the draft's priorities are approved on their own."""

        async def run() -> WorkQueueFillOut:
            filled = host.queues.from_priority(
                body.suggestion_id,
                queue_id=body.queue_id,
                name=body.name,
                description=body.description,
            )
            return WorkQueueFillOut.of(filled)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        "/queues/suggestions/split",
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def suggest_queue_split(
        body: WorkQueueSplitAskIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Ask the agent, in the background, which of these queues each of their open items
        fits; the ``queue_split`` draft carries the answer's ``scope``."""

        async def run() -> Response:
            return _ask_started(host.queues.start_split(body.queue_ids))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post("/queues/suggestions/{suggestion_id}/approve", response_model=WorkQueueApprovalOut)
    async def approve_queue_suggestion(
        suggestion_id: SuggestionId, idem: IdempotentCall = keyed
    ) -> Response:
        """Approve a queue proposal (``queue_next``, ``queue_order`` or ``queue_split``): it
        changes the local queues at once and never GitHub, skipping what changed since (each
        in ``skipped``). Any other kind is a 400; a settled one a 409."""

        async def run() -> WorkQueueApprovalOut:
            return WorkQueueApprovalOut.of(host.queues.approve(suggestion_id))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get(queue_path, response_model=WorkQueueOut)
    async def get_queue(queue_id: QueueId) -> Response:
        """One queue from the cache: its open items in order (each with its readiness and
        warning), the Done group and the priority bands."""

        async def run() -> WorkQueueOut:
            return WorkQueueOut.of(host.queues.queue(queue_id))

        return await _respond(run, host.clock)

    @r.patch(queue_path, response_model=WorkQueueOut)
    async def update_queue(
        queue_id: QueueId, body: WorkQueuePatchIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Rename a queue, change its description, or move it in the list of queues."""

        async def run() -> WorkQueueOut:
            changed = host.queues.update(
                queue_id, name=body.name, description=body.description, position=body.position
            )
            return WorkQueueOut.of(changed)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.delete(queue_path, status_code=status.HTTP_204_NO_CONTENT)
    async def delete_queue(queue_id: QueueId, idem: IdempotentCall = keyed) -> Response:
        """Delete a queue and its items (nothing on GitHub changes)."""

        async def run() -> Response:
            host.queues.delete(queue_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get(f"{queue_path}/warnings", response_model=WorkQueueWarningsOut)
    async def get_queue_warnings(queue_id: QueueId) -> Response:
        """The queue's open items that aren't ready for an agent, each with why."""

        async def run() -> WorkQueueWarningsOut:
            found = host.queues.warnings(queue_id)
            return WorkQueueWarningsOut(warnings=[WorkQueueWarningOut.of(w) for w in found])

        return await _respond(run, host.clock)

    @r.post(f"{queue_path}/items", response_model=WorkQueueOut, status_code=status.HTTP_201_CREATED)
    async def add_queue_item(
        queue_id: QueueId, body: WorkQueueItemIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Add a cached issue, epic or milestone at ``position`` (local, at once). An item in
        the queue already is a 409; one not cached a 404."""

        async def run() -> Response:
            view = host.queues.add(queue_id, body.kind, body.repo, body.n, body.position, body.note)
            return JSONResponse(
                jsonable_encoder(WorkQueueOut.of(view)), status_code=status.HTTP_201_CREATED
            )

        return await idem.run(lambda: _respond(run, host.clock))

    @r.patch(queue_item_path, response_model=WorkQueueOut)
    async def update_queue_item(
        queue_id: QueueId,
        kind: Kind,
        owner: Owner,
        repo: Name,
        n: Number,
        body: WorkQueueItemPatchIn,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Reorder an open item (``position`` among the open items) or change its note: local,
        at once, never GitHub."""

        async def run() -> WorkQueueOut:
            view = host.queues.update_item(
                queue_id, kind, _repo(owner, repo), n, position=body.position, note=body.note
            )
            return WorkQueueOut.of(view)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.delete(queue_item_path, response_model=WorkQueueOut)
    async def remove_queue_item(
        queue_id: QueueId,
        kind: Kind,
        owner: Owner,
        repo: Name,
        n: Number,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Take an item out of the queue (nothing on GitHub changes); answers the queue."""

        async def run() -> WorkQueueOut:
            return WorkQueueOut.of(host.queues.remove(queue_id, kind, _repo(owner, repo), n))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{queue_item_path}/done", response_model=WorkQueueActionOut)
    async def queue_item_done(
        queue_id: QueueId,
        kind: Kind,
        owner: Owner,
        repo: Name,
        n: Number,
        body: WorkQueueDoneIn,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Mark an item done at once and stage closing its issue (with the optional comment) or
        setting it ``agent-ready`` in the tray (``source`` ``queue``); GitHub changes when the
        tray is applied. A milestone is a 400 (Jesinia never writes milestones), a done item a
        409."""

        async def run() -> WorkQueueActionOut:
            action = host.queues.done(
                queue_id, kind, _repo(owner, repo), n, body.then, body.comment
            )
            staged = None if action.staged is None else PendingOut.of(action.staged)
            return WorkQueueActionOut(queue=WorkQueueOut.of(action.view), staged=staged)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{queue_item_path}/undo", response_model=WorkQueueOut)
    async def queue_item_undo(
        queue_id: QueueId,
        kind: Kind,
        owner: Owner,
        repo: Name,
        n: Number,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Undo Done: the item is open again in its old place, and its staged change leaves the
        tray if it is still there."""

        async def run() -> WorkQueueOut:
            return WorkQueueOut.of(host.queues.undo_done(queue_id, kind, _repo(owner, repo), n))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{queue_item_path}/band", response_model=WorkQueueActionOut)
    async def queue_item_band(
        queue_id: QueueId,
        kind: Kind,
        owner: Owner,
        repo: Name,
        n: Number,
        body: WorkQueueBandIn,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Drop an item into a priority band at ``position``: reordered at once, and, for
        another band, the priority label staged in the tray (``source`` ``queue``)."""

        async def run() -> WorkQueueActionOut:
            action = host.queues.move_to_band(
                queue_id, kind, _repo(owner, repo), n, body.band, body.position
            )
            staged = None if action.staged is None else PendingOut.of(action.staged)
            return WorkQueueActionOut(queue=WorkQueueOut.of(action.view), staged=staged)

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        f"{queue_path}/suggestions/next",
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def suggest_queue_next(queue_id: QueueId, idem: IdempotentCall = keyed) -> Response:
        """Ask the agent, in the background, which open issues to add next (ready,
        high-priority work first); the ``queue_next`` draft carries the answer's ``scope``."""

        async def run() -> Response:
            return _ask_started(host.queues.start_next(queue_id))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        f"{queue_path}/suggestions/order",
        response_model=IssueAskRunOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def suggest_queue_order(queue_id: QueueId, idem: IdempotentCall = keyed) -> Response:
        """Ask the agent, in the background, for an order of the queue's open items (by
        dependencies and priority); the ``queue_order`` draft carries the answer's ``scope``."""

        async def run() -> Response:
            return _ask_started(host.queues.start_order(queue_id))

        return await idem.run(lambda: _respond(run, host.clock))

    theme_path = "/themes/{tag}"

    @r.get("/themes", response_model=ThemesOut)
    async def get_themes() -> Response:
        """Every theme (a tag from the KB catalog) with its number of issues, by tag: the column
        of drop targets (local; no GitHub)."""

        async def run() -> ThemesOut:
            return ThemesOut(themes=[ThemeSummaryOut.of(t) for t in await host.themes.themes()])

        return await _respond(run, host.clock)

    @r.get(theme_path, response_model=ThemeOut)
    async def get_theme(tag: Tag) -> Response:
        """The theme's issues, by repo and number, from the cache (the theme as a filter). 404
        when no issue has it."""

        async def run() -> ThemeOut:
            name = await host.themes.canonical(tag)
            found = [ThemedIssueOut.of(i) for i in await host.themes.issues(name)]
            return ThemeOut(tag=name, issues=found)

        return await _respond(run, host.clock)

    @r.post(f"{theme_path}/issues", response_model=ThemeAddOut, status_code=status.HTTP_201_CREATED)
    async def add_theme(tag: Tag, body: ThemeIssuesIn, idem: IdempotentCall = keyed) -> Response:
        """Give the cached issues the theme, at once and locally (a drop; nothing reaches
        GitHub). A tag new to the KB catalog passes its catalog-only near-duplicate check (no
        agent, so a drop never waits) and is added to it; a near-duplicate is a 409 naming the
        catalog tags. ``added`` lists the issues that didn't have it yet, for Undo."""

        async def run() -> Response:
            added = await host.themes.add(body.refs(), tag)
            return JSONResponse(
                jsonable_encoder(ThemeAddOut.of(added)), status_code=status.HTTP_201_CREATED
            )

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(f"{theme_path}/issues/remove", response_model=ThemeRemoveOut)
    async def remove_theme(tag: Tag, body: ThemeIssuesIn, idem: IdempotentCall = keyed) -> Response:
        """Take the theme off these issues, locally (also an add's Undo). The tag stays in the KB
        catalog."""

        async def run() -> ThemeRemoveOut:
            return ThemeRemoveOut(removed=await host.themes.remove(body.refs(), tag))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post(
        f"{theme_path}/publish",
        response_model=ThemePublishOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def publish_theme(
        tag: Tag, body: ThemePublishIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Stage the theme's ``theme:<tag>`` label in ``repo`` in the pending tray: the label
        setup step and the label addition for each of its cached issues there that lacks it
        (steps already staged are reused). Nothing reaches GitHub until the tray is applied,
        and the label is created only by that setup step. 409 when no issue there needs it."""

        async def run() -> Response:
            published = await host.themes.publish(tag, body.repo)
            return JSONResponse(
                jsonable_encoder(ThemePublishOut.of(published)),
                status_code=status.HTTP_201_CREATED,
            )

        return await idem.run(lambda: _respond(run, host.clock))

    @r.get(f"{theme_path}/epic", response_model=ThemeEpicPlanOut)
    async def get_theme_epic(
        tag: Tag,
        repo: Annotated[str, Query(min_length=3, max_length=REPO_MAX)],
        new_epic: bool = False,
    ) -> Response:
        """What turning the theme into an epic in ``repo`` (one of its issues' repos) would do:
        its issues become the children (another repo's warned, "won't fit one PR"; another
        owner's refused), and what is skipped and why. Writes nothing. Once the theme has an
        epic, the plan links under it; a new epic is planned only with ``new_epic``, and a
        closed epic without it is a 409 (``ThemeEpicClosedError``: reopen it or ask for a new
        one)."""

        async def run() -> ThemeEpicPlanOut:
            name = await host.themes.canonical(tag)
            plan = await host.themes.to_epic(name, repo, new_epic=new_epic)
            return ThemeEpicPlanOut.of(name, plan)

        return await _respond(run, host.clock)

    @r.post(
        f"{theme_path}/epic",
        response_model=ApplyBatchOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def make_theme_epic(
        tag: Tag,
        body: ThemeEpicIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Approve the epic previewed with ``digest``: one batch (create the epic, then link the
        children) applied in the background, a 202 with the batch and ``Location`` to poll. A
        409 when the plan changed since (preview again) or has no children, or while an earlier
        epic batch for the theme runs or didn't finish (retry or abandon that batch: it may have
        made the epic), or when its epic is closed and ``new_epic`` isn't asked. Once the theme
        has an epic, issues added later link under it."""

        async def run() -> Response:
            started = await host.themes.start_epic(
                tag,
                body.repo,
                body.digest,
                _request_key("theme-epic", idem),
                host.apply,
                new_epic=body.new_epic,
            )
            return _started(started.id, ApplyBatchOut.of(started))

        return await idem.run(lambda: _respond(run, host.clock))

    @r.post("/pending/{batch}/abandon", response_model=AbandonedOut)
    async def abandon_batch(
        batch: ItemId,
        body: AbandonIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Give up on an unfinished batch of an origin that creates issues (a group or split, a
        conversion, a theme's epic), even one already pruned: it never runs again, and the
        issues it created (plus ``created``, confirmed on GitHub for a create cut off while
        being sent) are reused by the next approval for its origin, which creates only what is
        missing. A batch still running is a 409, a complete one a 400."""

        async def run() -> AbandonedOut:
            confirmed = {c.temp_id: (c.repo, c.n) for c in body.created}
            return AbandonedOut.of(*host.apply.abandon(batch, confirmed))

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
