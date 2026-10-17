# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review routes under ``/api/review``: the queue, opened PRs, drafts, posting, settings.

Every route needs the owner (the app mounts module routers behind ``require_owner``);
posting also declares it itself, since it is the one route that writes to GitHub. Every
write takes an ``Idempotency-Key``. The queue and PR reads serve ``review.sqlite`` only and
never wait for GitHub (spec §2, "Always cached"); a running or failed sync is reported in
the response. Typed errors become ``{"error", "detail"}`` JSON with their status.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from jesinia.apps.review.api.schemas import (
    REPO_MAX,
    DraftsIn,
    DraftsOut,
    LoadIn,
    OpenOut,
    PostedOut,
    PostIn,
    PreviewIn,
    PreviewOut,
    PrOut,
    QueueOut,
    QueueRowOut,
    ReviewErrorOut,
    ReviewSettingsBody,
    RoundOut,
)
from jesinia.apps.review.application.pipeline import OpenResult, Pipeline
from jesinia.apps.review.application.posting import PostingFailed, PostingService
from jesinia.apps.review.application.queue import QueueService
from jesinia.apps.review.application.reviews import ReviewReads
from jesinia.apps.review.domain.posting import (
    CommentOnlyOnOwnPr,
    EmptyReview,
    InvalidDraftError,
    NeedsConfirmation,
    NoReviewRound,
    PostingDomainError,
    PreviewOutdated,
    ReviewedHeadGone,
)
from jesinia.apps.review.domain.queue import (
    InvalidPrRefError,
    InvalidSettingError,
    PrHeadMovedError,
    PrNotInQueueError,
    PrNotOpenError,
    PrRef,
    QueueDomainError,
    QueueFilter,
    ReviewNotAllowedError,
)
from jesinia.core.auth import Principal, require_owner
from jesinia.core.idempotency import IdempotencyStore, IdempotentCall, idempotent
from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.pr import PullRequestDomainError


class ReviewHost(Protocol):
    """What the routes need from the module (each property answers 503 until it can)."""

    @property
    def queue(self) -> QueueService: ...

    @property
    def pipeline(self) -> Pipeline: ...

    @property
    def posting(self) -> PostingService: ...

    @property
    def reviews(self) -> ReviewReads: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...


_STATUS: dict[type[Exception], int] = {
    PrNotInQueueError: status.HTTP_404_NOT_FOUND,
    NoReviewRound: status.HTTP_404_NOT_FOUND,
    InvalidPrRefError: status.HTTP_400_BAD_REQUEST,
    InvalidSettingError: status.HTTP_400_BAD_REQUEST,
    InvalidDraftError: status.HTTP_400_BAD_REQUEST,
    EmptyReview: status.HTTP_400_BAD_REQUEST,
    PrNotOpenError: status.HTTP_409_CONFLICT,
    ReviewNotAllowedError: status.HTTP_409_CONFLICT,
    PrHeadMovedError: status.HTTP_409_CONFLICT,
    NeedsConfirmation: status.HTTP_409_CONFLICT,
    ReviewedHeadGone: status.HTTP_409_CONFLICT,
    PreviewOutdated: status.HTTP_409_CONFLICT,
    CommentOnlyOnOwnPr: status.HTTP_409_CONFLICT,
    # GitHub, not this server, failed: the cause is in ``detail``.
    GitHubUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubRateLimitedError: status.HTTP_503_SERVICE_UNAVAILABLE,
    PostingFailed: status.HTTP_502_BAD_GATEWAY,
    GitHubError: status.HTTP_502_BAD_GATEWAY,
    # Anything else the request itself got wrong.
    QueueDomainError: status.HTTP_400_BAD_REQUEST,
    PostingDomainError: status.HTTP_400_BAD_REQUEST,
    PullRequestDomainError: status.HTTP_400_BAD_REQUEST,
}
_TYPED = (
    QueueDomainError,
    PostingDomainError,
    GitHubError,
    PullRequestDomainError,
)
# ``Any`` is FastAPI's own type for ``responses`` (free-form OpenAPI fragments).
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ReviewErrorOut} for code in (400, 404, 409, 502, 503)
}

Owner = Annotated[str, Path(min_length=1, max_length=REPO_MAX)]
Name = Annotated[str, Path(min_length=1, max_length=REPO_MAX)]
Number = Annotated[int, Path(ge=1, le=999_999_999)]


def _status_of(exc: Exception) -> int:
    """The status of the most specific mapped class (every ``_TYPED`` base is mapped)."""
    return next(_STATUS[kind] for kind in type(exc).__mro__ if kind in _STATUS)


def _error(exc: Exception) -> JSONResponse:
    warnings = list(exc.warnings) if isinstance(exc, NeedsConfirmation) else None
    body = ReviewErrorOut(error=type(exc).__name__, detail=str(exc), warnings=warnings)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=_status_of(exc))


async def _respond(
    work: Callable[[], Awaitable[BaseModel | None]], ok: int = status.HTTP_200_OK
) -> Response:
    """``work``'s model as a concrete ``Response`` (so idempotency can store it), or its
    typed error as structured JSON."""
    try:
        result = await work()
    except _TYPED as exc:
        return _error(exc)
    if result is None:
        return Response(status_code=ok)
    return JSONResponse(jsonable_encoder(result), status_code=ok)


def _ref(owner: str, repo: str, number: int) -> PrRef:
    """Raises: InvalidPrRefError: not a valid ``owner/name`` and number."""
    return PrRef(f"{owner}/{repo}", number)


def _opened(result: OpenResult) -> Response:
    code = status.HTTP_202_ACCEPTED if result.state == "started" else status.HTTP_200_OK
    return JSONResponse(jsonable_encoder(OpenOut.of(result)), status_code=code)


def build_router(host: ReviewHost) -> APIRouter:
    r = APIRouter(prefix="/api/review", tags=["review"], responses=_ERRORS)
    keyed = Depends(idempotent(lambda: host.idempotency))
    pr_path = "/prs/{owner}/{repo}/{number}"

    @r.get("/queue", response_model=QueueOut)
    async def get_queue(
        repo: Annotated[str | None, Query(max_length=REPO_MAX)] = None,
        include_dismissed: bool = False,
    ) -> Response:
        """The cached queue, at once; ``syncing`` and ``sync_error`` report GitHub."""

        async def run() -> QueueOut:
            view = await host.queue.list(QueueFilter(repo, include_dismissed))
            return QueueOut.of(view)

        return await _respond(run)

    @r.post("/load", response_model=QueueRowOut)
    async def load(body: LoadIn, idem: IdempotentCall = keyed) -> Response:
        """Load a PR for review now (from the cache, else from GitHub)."""

        async def run() -> QueueRowOut:
            return QueueRowOut.of(await host.queue.load(body.ref))

        return await idem.run(lambda: _respond(run))

    @r.post(
        "/queue/{owner}/{repo}/{number}/dismiss",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def dismiss(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        async def run() -> None:
            ref = _ref(owner, repo, number)
            await host.queue.dismiss(ref.repo, ref.number)

        return await idem.run(lambda: _respond(run, status.HTTP_204_NO_CONTENT))

    @r.get(pr_path, response_model=PrOut)
    async def get_pr(owner: Owner, repo: Name, number: Number) -> Response:
        """The PR's latest round and its artifacts, from the cache only."""

        async def run() -> PrOut:
            return PrOut.of(host.reviews.pr(_ref(owner, repo, number)))

        return await _respond(run)

    async def _open(
        idem: IdempotentCall,
        start: Callable[[PrRef], Awaitable[OpenResult]],
        owner: str,
        repo: str,
        number: int,
    ) -> Response:
        async def run() -> Response:
            try:
                return _opened(await start(_ref(owner, repo, number)))
            except _TYPED as exc:
                return _error(exc)

        return await idem.run(run)

    open_responses: dict[int | str, dict[str, Any]] = {
        202: {"model": OpenOut, "description": "Jobs started"}
    }

    @r.post(f"{pr_path}/open", response_model=OpenOut, responses=open_responses)
    async def open_pr(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Open the PR: start what its round lacks (202), or say why not (gates)."""
        return await _open(idem, lambda ref: host.pipeline.open(ref), owner, repo, number)

    @r.post(f"{pr_path}/retry", response_model=OpenOut, responses=open_responses)
    async def retry(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Run again what failed: a failed round gets a new one, failed jobs run again."""
        return await _open(idem, lambda ref: host.pipeline.open(ref), owner, repo, number)

    @r.post(f"{pr_path}/review-anyway", response_model=OpenOut, responses=open_responses)
    async def review_anyway(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Open without the CI and automated-review gates; failing CI still never approves."""
        return await _open(idem, lambda ref: host.pipeline.review_anyway(ref), owner, repo, number)

    @r.get(f"{pr_path}/drafts", response_model=DraftsOut)
    async def get_drafts(owner: Owner, repo: Name, number: Number) -> Response:
        """The latest round's summary and drafts (seeded from its findings the first time)."""

        async def run() -> DraftsOut:
            round_id = await host.posting.round_for(_ref(owner, repo, number))
            return DraftsOut.of(await host.posting.drafts(round_id))

        return await _respond(run)

    @r.put(f"{pr_path}/drafts", response_model=DraftsOut)
    async def put_drafts(
        owner: Owner, repo: Name, number: Number, body: DraftsIn, idem: IdempotentCall = keyed
    ) -> Response:
        """Replace the latest round's summary and drafts (deleted ones stay deleted)."""

        async def run() -> DraftsOut:
            round_id = await host.posting.round_for(_ref(owner, repo, number))
            drafts = [draft.draft() for draft in body.drafts]
            return DraftsOut.of(await host.posting.save_drafts(round_id, body.summary, drafts))

        return await idem.run(lambda: _respond(run))

    @r.post(f"{pr_path}/post/preview", response_model=PreviewOut)
    async def preview(owner: Owner, repo: Name, number: Number, body: PreviewIn) -> Response:
        """Exactly what posting would send, its warnings and the digest to post it with."""
        # No Idempotency-Key: a POST only for its body; it is safe to repeat (its one write,
        # seeding the drafts, happens once whatever the number of calls).

        async def run() -> PreviewOut:
            round_id = await host.posting.round_for(_ref(owner, repo, number))
            return PreviewOut.of(await host.posting.preview(round_id, body.event))

        return await _respond(run)

    @r.post(f"{pr_path}/post", response_model=PostedOut)
    async def post(
        owner: Owner,
        repo: Name,
        number: Number,
        body: PostIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Post the previewed review to GitHub as one atomic review on the round's head.
        Owner only (never an agent session); every preview warning must be confirmed."""

        async def run() -> PostedOut:
            round_id = await host.posting.round_for(_ref(owner, repo, number))
            url = await host.posting.post(
                round_id, body.event, body.confirm, key=idem.key or "", digest=body.digest
            )
            return PostedOut(review_url=url)

        return await idem.run(lambda: _respond(run))

    @r.post(f"{pr_path}/done", response_model=RoundOut)
    async def done(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Mark the latest round done without posting."""

        async def run() -> RoundOut:
            return RoundOut.of(host.reviews.done(_ref(owner, repo, number)))

        return await idem.run(lambda: _respond(run))

    @r.get("/settings", response_model=ReviewSettingsBody)
    async def get_settings() -> ReviewSettingsBody:
        return ReviewSettingsBody.of(host.reviews.settings())

    @r.put("/settings", response_model=ReviewSettingsBody)
    async def put_settings(body: ReviewSettingsBody, idem: IdempotentCall = keyed) -> Response:
        """Save Settings → Review; repos not listed keep their settings."""

        async def run() -> ReviewSettingsBody:
            repos = [repo.config() for repo in body.repos]
            saved = host.reviews.update_settings(body.prereview, body.runner, repos)
            return ReviewSettingsBody.of(saved)

        return await idem.run(lambda: _respond(run))

    return r
