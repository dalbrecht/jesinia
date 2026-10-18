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
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from jesinia.apps.review.api.schemas import (
    REPO_MAX,
    DoneIn,
    DraftsIn,
    DraftsOut,
    LoadIn,
    OpenIn,
    OpenOut,
    PostAllIn,
    PostAllOut,
    PostedOut,
    PostIn,
    PostOutcomeOut,
    PreviewIn,
    PreviewOut,
    PrOut,
    QueueOut,
    QueueRowOut,
    ReviewErrorOut,
    ReviewSettingsBody,
    RoundOut,
    StackOpenIn,
    StackOpenOut,
    StackOut,
)
from jesinia.apps.review.application.pipeline import OpenResult, Pipeline
from jesinia.apps.review.application.posting import PostingFailed, PostingService
from jesinia.apps.review.application.queue import QueueService
from jesinia.apps.review.application.reviews import ReviewReads
from jesinia.apps.review.application.stacks import StackReviews
from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.gates import GateInputError
from jesinia.apps.review.domain.posting import (
    CommentOnlyOnOwnPr,
    EarlierReviewPosted,
    EmptyReview,
    InvalidDraftContent,
    InvalidDraftError,
    NeedsConfirmation,
    NoReviewRound,
    PostingDomainError,
    PostingInProgress,
    PostingStillUncertain,
    PreviewOutdated,
    ReviewBodyTooLong,
    ReviewedHeadGone,
    RoundChanged,
    RoundFrozen,
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
from jesinia.apps.review.domain.stacks import (
    InvalidPostAllError,
    NotAStackError,
    PostAllStopped,
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
    def stacks(self) -> StackReviews: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...


_STATUS: dict[type[Exception], int] = {
    PrNotInQueueError: status.HTTP_404_NOT_FOUND,
    NoReviewRound: status.HTTP_404_NOT_FOUND,
    NotAStackError: status.HTTP_404_NOT_FOUND,
    InvalidPostAllError: status.HTTP_400_BAD_REQUEST,
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
    RoundChanged: status.HTTP_409_CONFLICT,
    CommentOnlyOnOwnPr: status.HTTP_409_CONFLICT,
    EarlierReviewPosted: status.HTTP_409_CONFLICT,
    PostingInProgress: status.HTTP_409_CONFLICT,
    PostingStillUncertain: status.HTTP_409_CONFLICT,
    InvalidDraftContent: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ReviewBodyTooLong: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # GitHub, not this server, failed: the cause is in ``detail``.
    GitHubUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    GitHubRateLimitedError: status.HTTP_503_SERVICE_UNAVAILABLE,
    PostingFailed: status.HTTP_502_BAD_GATEWAY,
    GitHubError: status.HTTP_502_BAD_GATEWAY,
    # Anything else the request itself got wrong.
    QueueDomainError: status.HTTP_400_BAD_REQUEST,
    PostingDomainError: status.HTTP_400_BAD_REQUEST,
    PullRequestDomainError: status.HTTP_400_BAD_REQUEST,
    GateInputError: status.HTTP_400_BAD_REQUEST,
    # The app's own failure (a workspace or job error, say): still typed, structured JSON.
    ReviewError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}
_TYPED = (
    ReviewError,
    GitHubError,
    PullRequestDomainError,
)
# ``Any`` is FastAPI's own type for ``responses`` (free-form OpenAPI fragments).
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ReviewErrorOut} for code in (400, 404, 409, 422, 500, 502, 503)
}

Owner = Annotated[str, Path(min_length=1, max_length=REPO_MAX)]
Name = Annotated[str, Path(min_length=1, max_length=REPO_MAX)]
Number = Annotated[int, Path(ge=1, le=999_999_999)]


def _status_of(exc: Exception) -> int:
    """The status of the most specific mapped class (every ``_TYPED`` base is mapped)."""
    return next(_STATUS[kind] for kind in type(exc).__mro__ if kind in _STATUS)


def _error(exc: Exception) -> JSONResponse:
    outcomes = None
    if isinstance(exc, PostAllStopped):
        # The PR that stopped it answers (its error, status and fields), with every outcome.
        outcomes = [PostOutcomeOut.of(o) for o in exc.outcomes]
        exc = exc.cause
    warnings = list(exc.warnings) if isinstance(exc, NeedsConfirmation) else None
    earlier = exc if isinstance(exc, EarlierReviewPosted) else None
    refused = exc if isinstance(exc, InvalidDraftContent) else None
    frozen = exc if isinstance(exc, RoundFrozen) else None
    body = ReviewErrorOut(
        error=type(exc).__name__,
        detail=str(exc),
        warnings=warnings,
        review_url=None if earlier is None else earlier.url,
        event=None if earlier is None else earlier.event,
        field=None if refused is None else refused.field,
        draft_index=None if refused is None else refused.draft_index,
        successor_round_id=None if frozen is None else frozen.successor_id,
        # Transient: the Retry-After header also keeps idempotency from replaying it (R50).
        retry_after=exc.retry_after
        if isinstance(exc, PostingStillUncertain | PostingInProgress)
        else None,
        stack_outcomes=outcomes,
    )
    headers = None if body.retry_after is None else {"Retry-After": str(body.retry_after)}
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=_status_of(exc), headers=headers
    )


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


def _shown(current: int, shown: int | None) -> int:
    """The round a save, preview or post acts on: the one the author was shown, which must
    still be the PR's current round (Codex P1 on PR #58). Never retargeted onto a newer one.

    Raises:
        RoundChanged: a newer round started since.
    """
    if shown is not None and shown != current:
        raise RoundChanged(
            f"a new review round ({current}) started since round {shown} was shown; nothing "
            "was saved or posted"
        )
    return current


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

    @r.post(
        "/repos/{owner}/{repo}/confirm",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def confirm_repo(owner: Owner, repo: Name, idem: IdempotentCall = keyed) -> Response:
        """Confirm a suggested repo (ruling R29a): it is watched and pre-reviewed from now on."""

        async def run() -> None:
            await host.queue.confirm_repo(f"{owner}/{repo}")

        return await idem.run(lambda: _respond(run, status.HTTP_204_NO_CONTENT))

    @r.get(pr_path, response_model=PrOut)
    async def get_pr(owner: Owner, repo: Name, number: Number) -> Response:
        """The PR's latest round and its artifacts, from the cache only."""

        async def run() -> PrOut:
            return PrOut.of(host.reviews.pr(_ref(owner, repo, number)))

        return await _respond(run)

    @r.post(f"{pr_path}/refresh", response_model=QueueRowOut)
    async def refresh_pr(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Fetch the PR from GitHub now (its new head, CI and reviews) and check the new head
        against your previous review; open it afterwards, under its own gates."""

        async def run() -> QueueRowOut:
            return QueueRowOut.of(await host.queue.refresh(_ref(owner, repo, number)))

        return await idem.run(lambda: _respond(run))

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
        owner: Owner,
        repo: Name,
        number: Number,
        body: OpenIn | None = None,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Open the PR: start what its round lacks (202), or say why not (gates). The body
        may choose the depth, or ask to walk everything."""
        asked = body or OpenIn()

        def start(ref: PrRef) -> Awaitable[OpenResult]:
            return host.pipeline.open(
                ref, depth=asked.depth, walk_everything=asked.walk_everything, quiz=asked.quiz
            )

        return await _open(idem, start, owner, repo, number)

    @r.post(f"{pr_path}/retry", response_model=OpenOut, responses=open_responses)
    async def retry(
        owner: Owner, repo: Name, number: Number, idem: IdempotentCall = keyed
    ) -> Response:
        """Run again what failed: a failed round gets a new one, failed jobs run again."""
        return await _open(idem, lambda ref: host.pipeline.retry(ref), owner, repo, number)

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
            round_id = _shown(
                await host.posting.round_for(_ref(owner, repo, number)), body.round_id
            )
            drafts = [draft.draft() for draft in body.drafts]
            return DraftsOut.of(await host.posting.save_drafts(round_id, body.summary, drafts))

        return await idem.run(lambda: _respond(run))

    @r.post(f"{pr_path}/post/preview", response_model=PreviewOut)
    async def preview(owner: Owner, repo: Name, number: Number, body: PreviewIn) -> Response:
        """Exactly what posting would send, its warnings and the digest to post it with."""
        # No Idempotency-Key: a POST only for its body; it is safe to repeat (its one write,
        # seeding the drafts, happens once whatever the number of calls).

        async def run() -> PreviewOut:
            round_id = _shown(
                await host.posting.round_for(_ref(owner, repo, number)), body.round_id
            )
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
            round_id = _shown(
                await host.posting.round_for(_ref(owner, repo, number)), body.round_id
            )
            url = await host.posting.post(
                round_id, body.event, body.confirm, key=idem.key or "", digest=body.digest
            )
            return PostedOut(review_url=url)

        return await idem.run(lambda: _respond(run))

    @r.post(f"{pr_path}/done", response_model=RoundOut)
    async def done(
        owner: Owner,
        repo: Name,
        number: Number,
        body: DoneIn | None = None,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Mark the latest round done without posting (the round shown, when the body names
        it: a newer one answers 409 RoundChanged)."""
        shown = None if body is None else body.round_id

        async def run() -> RoundOut:
            return RoundOut.of(host.reviews.done(_ref(owner, repo, number), shown))

        return await idem.run(lambda: _respond(run))

    stack_path = "/stacks/{owner}/{repo}/{number}"

    @r.get(stack_path, response_model=StackOut)
    async def get_stack(owner: Owner, repo: Name, number: Number) -> Response:
        """The stack holding the PR (spec §4.3), from the cache only: each PR's view, the
        stack review of their heads, and the cross-PR findings."""

        async def run() -> StackOut:
            return StackOut.of(host.stacks.view(_ref(owner, repo, number)))

        return await _respond(run)

    stack_open_responses: dict[int | str, dict[str, Any]] = {
        202: {"model": StackOpenOut, "description": "Stack jobs started"}
    }

    @r.post(f"{stack_path}/open", response_model=StackOpenOut, responses=stack_open_responses)
    async def open_stack(
        owner: Owner,
        repo: Name,
        number: Number,
        body: StackOpenIn | None = None,
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Open the stack: open each PR (under its own gates), then start the stack's
        Overview, Background and stack walkthrough it lacks (202) once no PR is held."""
        retry = body is not None and body.retry

        async def run() -> Response:
            try:
                result = await host.stacks.open(_ref(owner, repo, number), retry=retry)
            except _TYPED as exc:
                return _error(exc)
            code = status.HTTP_202_ACCEPTED if result.state == "started" else status.HTTP_200_OK
            return JSONResponse(jsonable_encoder(StackOpenOut.of(result)), status_code=code)

        return await idem.run(run)

    @r.post(f"{stack_path}/post-all", response_model=PostAllOut)
    async def post_all(
        owner: Owner,
        repo: Name,
        number: Number,
        body: PostAllIn,
        _owner: Annotated[Principal, Depends(require_owner)],
        idem: IdempotentCall = keyed,
    ) -> Response:
        """Post every PR's confirmed review, bottom-up, stopping at the first that can't be
        posted (its error, with ``stack_outcomes``). Owner only; each PR's attempt is keyed
        from this request's key, so a replay never posts a PR twice."""

        async def run() -> PostAllOut:
            result = await host.stacks.post_all(
                _ref(owner, repo, number),
                [item.item() for item in body.items],
                # ``idem.run`` refuses a request without a key before this runs.
                key=cast(str, idem.key),
            )
            return PostAllOut(outcomes=[PostOutcomeOut.of(o) for o in result.outcomes])

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
