# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub REST/GraphQL access with ETag revalidation (spec §4.5).

The token comes from ``gh`` or the Keychain and is held in memory only, for at most
``TOKEN_TTL_SECONDS``; it is never written anywhere or logged. A 401 drops it and retries
once with a fresh one. One HTTP client (and its connection pool) serves every request until
``aclose``. A 304 answer is served from ``github.sqlite`` (304s do not count against the
rate limit). A rate-limited answer (429, or 403 with no quota left or a ``Retry-After``)
raises ``GitHubRateLimitedError`` with the time GitHub says the limit resets.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

import httpx

from jesinia.services.github.application.ports import GitHubCache, TokenSource
from jesinia.services.github.domain.pr import (
    DraftComment,
    ReviewEvent,
    validate_pull_number,
    validate_repo,
    validate_sha,
)

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(20.0)
# `gh auth token` costs a process start per call; an inbox sync makes hundreds of requests.
TOKEN_TTL_SECONDS = 300.0

WriteMethod = Literal["POST", "PATCH", "PUT", "DELETE"]


class GitHubError(RuntimeError):
    """Base for GitHub service failures."""


class GitHubUnavailable(GitHubError):  # noqa: N818 - public name fixed by the plan interface
    """No usable token: GitHub features are off until ``gh auth login`` (or a Keychain PAT)."""


class GitHubRequestError(GitHubError):
    """GitHub refused or failed a request; carries its status and its own message."""

    def __init__(self, status: int | None, github_message: str) -> None:
        answered = f"GitHub answered {status}" if status is not None else "GitHub request failed"
        super().__init__(f"{answered}: {github_message}")
        self.status = status
        self.github_message = github_message


class UnsafeGitHubPathError(GitHubError):
    """A request path that would reach a different resource than the one written (a dot
    segment, an unencoded character), or a write outside ``/repos/``; refused unsent."""


class GitHubRateLimitedError(GitHubRequestError):
    """GitHub is rate limiting this token; ``reset_at`` is when it says to try again
    (``None`` when it didn't say)."""

    def __init__(self, status: int, github_message: str, reset_at: datetime | None) -> None:
        until = f" (rate limited until {reset_at.isoformat()})" if reset_at else " (rate limited)"
        super().__init__(status, github_message + until)
        self.reset_at = reset_at


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    data: object
    from_cache: bool


def _json(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_rate_limited(resp: httpx.Response) -> bool:
    """429 always; 403 only with GitHub's rate-limit signs, since a plain 403 is a refusal."""
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and (
        resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers
    )


def _reset_at(resp: httpx.Response, now: datetime) -> datetime | None:
    """``Retry-After`` (seconds from now) wins over ``x-ratelimit-reset`` (epoch seconds),
    as GitHub's docs advise for secondary limits."""
    retry_after = resp.headers.get("retry-after", "")
    if retry_after.isdigit():
        return now + timedelta(seconds=int(retry_after))
    reset = resp.headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return datetime.fromtimestamp(int(reset), UTC)
    return None


def _error_detail(error: object) -> str:
    if isinstance(error, Mapping):
        parts = (error.get(key) for key in ("message", "resource", "field", "code"))
        return " ".join(str(part) for part in parts if part)
    return str(error)


def github_error_message(data: object) -> str:
    """GitHub's ``message`` plus each entry of its ``errors`` list, where the real cause
    of a 422 lives; a non-JSON body is returned as is."""
    if not isinstance(data, Mapping):
        return str(data)
    message = str(data.get("message") or "no message")
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        message += ": " + "; ".join(_error_detail(e) for e in errors)
    return message


def _comment_payload(comment: DraftComment) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": comment.path,
        "line": comment.line,
        "side": comment.side,
        "body": comment.body,
    }
    if comment.start_line is not None:
        payload["start_line"] = comment.start_line
        payload["start_side"] = comment.start_side
    return payload


class GitHubService:
    """Shared GitHub access.

    Args:
        token: where a token comes from (``gh``, then the Keychain).
        cache: ETag cache for GETs.
        transport: an httpx transport for tests.
        clock: seconds, monotonic; for tests of the token's lifetime.
        wall_clock: the current UTC time, to turn ``Retry-After`` into a reset time.
    """

    def __init__(
        self,
        token: TokenSource,
        cache: GitHubCache,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._token = token
        self._wall_clock = wall_clock
        self._cache = cache
        self._transport = transport
        self._clock = clock
        self._cached_token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self.closed = False

    async def aclose(self) -> None:
        """Close the shared HTTP client; safe to call more than once."""
        self.closed = True
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _current_token(self, *, refresh: bool = False) -> str:
        """The in-memory token, looked up again when it is older than the TTL or when
        GitHub rejected it. Concurrent callers share one lookup."""
        async with self._token_lock:
            if refresh or self._cached_token is None or self._clock() >= self._token_expires:
                self._cached_token = None
                token = await self._token()
                if not token:
                    raise GitHubUnavailable(
                        "no GitHub token: run `gh auth login`, or store a PAT in the Keychain "
                        "as `jesinia-github`"
                    )
                self._cached_token = token
                self._token_expires = self._clock() + TOKEN_TTL_SECONDS
            return self._cached_token

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=API, timeout=TIMEOUT, transport=self._transport, trust_env=False
            )
        return self._http

    async def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        """One request, authorized with the in-memory token. A 401 drops the token and
        retries once with a fresh one; a network failure becomes a typed error that keeps
        httpx's cause, and a rate limit raises ``GitHubRateLimitedError``."""
        client = self._client()
        # Built once without credentials to check the path httpx will really send: it drops
        # dot segments and encodes some characters, either of which changes the resource.
        sent_path = client.build_request(method, path).url.raw_path
        if sent_path != path.encode("ascii", "replace"):
            raise UnsafeGitHubPathError(f"refused a request path that changes when sent: {path!r}")
        for attempt in range(2):
            token = await self._current_token(refresh=attempt > 0)
            try:
                resp = await client.request(
                    method,
                    path,
                    params=params,
                    json=None if payload is None else dict(payload),
                    headers={**headers, "Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise GitHubRequestError(None, str(exc) or type(exc).__name__) from exc
            if _is_rate_limited(resp):
                raise GitHubRateLimitedError(
                    resp.status_code,
                    github_error_message(_json(resp)),
                    _reset_at(resp, self._wall_clock()),
                )
            if resp.status_code != 401:
                return resp
        raise GitHubUnavailable("GitHub rejected the token: run `gh auth login` again")

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> GitHubResponse:
        headers = self._headers()
        query = urlencode(sorted((params or {}).items()))
        key = f"{path}?{query}" if query else path
        cached = self._cache.get(key)
        if cached is not None:
            headers["If-None-Match"] = cached.etag
        resp = await self._send("GET", path, headers, params=params)
        if resp.status_code == 304 and cached is not None:
            return GitHubResponse(cached.status, cached.data, True)
        data = _json(resp)
        etag = resp.headers.get("ETag")
        if resp.is_success and etag:
            self._cache.put(key, etag, resp.status_code, data)
        return GitHubResponse(resp.status_code, data, False)

    async def graphql(self, query: str, variables: Mapping[str, object]) -> dict[str, object]:
        headers = self._headers()
        resp = await self._send(
            "POST", "/graphql", headers, payload={"query": query, "variables": dict(variables)}
        )
        body = _json(resp)
        if not isinstance(body, Mapping):
            raise GitHubRequestError(resp.status_code, str(body))
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            raise GitHubRequestError(resp.status_code, "; ".join(_error_detail(e) for e in errors))
        if not resp.is_success:
            raise GitHubRequestError(resp.status_code, github_error_message(body))
        data = body.get("data")
        return {str(k): v for k, v in data.items()} if isinstance(data, Mapping) else {}

    async def _write(
        self, method: WriteMethod, path: str, payload: Mapping[str, object] | None = None
    ) -> object:
        """The one write path: never cached or revalidated, sent through ``_send`` (so the
        token cache, 401 refresh and rate-limit handling apply), and every failure keeps
        GitHub's cause. An empty answer (a 204) comes back as ``""``. Writes only reach
        repository resources: any path outside ``/repos/`` is refused unsent."""
        if not path.startswith("/repos/"):
            raise UnsafeGitHubPathError(f"writes must target /repos/..., got {path!r}")
        resp = await self._send(method, path, self._headers(), payload=payload)
        data = _json(resp)
        if not resp.is_success:
            raise GitHubRequestError(resp.status_code, github_error_message(data))
        return data

    async def write(
        self, method: WriteMethod, path: str, payload: Mapping[str, object] | None = None
    ) -> object:
        """A write for this service's typed clients (``IssueClient``); see ``_write``.

        The owner check is the caller's: routes refuse session principals before calling.
        """
        return await self._write(method, path, payload)

    async def _post(self, path: str, payload: Mapping[str, object]) -> object:
        return await self._write("POST", path, payload)

    async def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_sha: str,
        event: ReviewEvent,
        body: str,
        comments: Sequence[DraftComment],
    ) -> str:
        """Post one atomic review (body plus every inline comment) and return its URL.

        Owner-only: the REST route enforces that, and no MCP tool may ever call this.

        Raises:
            GitHubUnavailable: no token, or GitHub rejected it.
            GitHubRequestError: GitHub refused the review; ``github_message`` says why.
            InvalidPullRequestReferenceError: ``repo``, ``number`` or ``commit_sha`` is malformed.
        """
        path = f"/repos/{validate_repo(repo)}/pulls/{validate_pull_number(number)}/reviews"
        data = await self._post(
            path,
            {
                "commit_id": validate_sha(commit_sha),
                "event": event,
                "body": body,
                "comments": [_comment_payload(c) for c in comments],
            },
        )
        url = data.get("html_url") if isinstance(data, Mapping) else None
        if not isinstance(url, str):
            raise GitHubRequestError(None, "GitHub created the review but returned no html_url")
        return url
