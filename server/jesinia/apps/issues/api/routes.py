# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issues routes under ``/api/issues``: watched repos and their Settings, the Inbox and
Backlog, one issue, and the label setup and migration batches.

Every route needs the owner: the app mounts module routers behind ``require_owner``, so agent
session tokens are refused (IR2); applying a label batch, the one approval here, also declares
it itself. Every write takes an ``Idempotency-Key``. GET routes serve ``issues.sqlite`` only and
never await GitHub (spec §6.3). A preview is a POST only for its body and isn't keyed: it reads
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
    IssueDetailOut,
    IssuesErrorOut,
    IssuesRepoIn,
    IssuesRepoOut,
    IssuesReposOut,
    IssueViewOut,
    LabelApplyIn,
    LabelApplyOut,
    LabelPlanIn,
    LabelPlanOut,
)
from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.sync import SYNC_INTERVAL, SyncService
from jesinia.apps.issues.domain.labels import (
    LabelNameCollisionError,
    LabelPlanError,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanNotFoundError,
    LabelPlanOutdatedError,
)
from jesinia.apps.issues.domain.schema import InvalidSettingsSchemaError, validate_settings_schema
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
    # GitHub, not this server, failed: the cause is in ``detail``.
    GitHubUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubRateLimitedError: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubError: status.HTTP_502_BAD_GATEWAY,
    # Anything else the request itself got wrong.
    TriageDomainError: status.HTTP_400_BAD_REQUEST,
    LabelPlanError: status.HTTP_400_BAD_REQUEST,
    InvalidSettingsSchemaError: status.HTTP_400_BAD_REQUEST,
    GitHubReferenceError: status.HTTP_400_BAD_REQUEST,
    IssueDomainError: status.HTTP_400_BAD_REQUEST,
}
_TYPED = (
    TriageDomainError,
    LabelPlanError,
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

    # Last: ``/{view}`` would otherwise also match ``/repos``.
    @r.get("/{view}", response_model=IssueViewOut)
    async def get_view(
        view: Annotated[str, Path(max_length=32)],
        repo: Annotated[str | None, Query(max_length=REPO_MAX)] = None,
        saved: Annotated[str | None, Query(alias="filter", max_length=64)] = None,
    ) -> Response:
        """The cached Inbox or Backlog, at once, even without the github service;
        ``syncing`` and ``sync_error`` report GitHub. ``filter`` is a saved filter
        (``incomplete``, ``stale_90d``)."""

        async def run() -> IssueViewOut:
            filters = IssueFilters(repo, parse_saved_filter(saved))
            return IssueViewOut.of(host.queries.view(parse_view(view), filters))

        return await _respond(run, host.clock)

    return r
