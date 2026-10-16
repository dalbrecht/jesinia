# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub REST/GraphQL access with ETag revalidation (spec §4.5).

The token comes from ``gh`` or the Keychain on every call and is never stored. A 304
answer is served from ``github.sqlite`` (304s do not count against the rate limit).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    def __init__(
        self,
        token: TokenSource,
        cache: GitHubCache,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._cache = cache
        self._transport = transport

    async def _headers(self) -> dict[str, str]:
        token = await self._token()
        if not token:
            raise GitHubUnavailable(
                "no GitHub token: run `gh auth login`, or store a PAT in the Keychain "
                "as `jesinia-github`"
            )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API, timeout=TIMEOUT, transport=self._transport, trust_env=False
        )

    async def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        """One request; a network failure becomes a typed error that keeps httpx's cause."""
        try:
            async with self._client() as client:
                resp = await client.request(
                    method,
                    path,
                    params=params,
                    json=None if payload is None else dict(payload),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise GitHubRequestError(None, str(exc) or type(exc).__name__) from exc
        if resp.status_code == 401:
            raise GitHubUnavailable("GitHub rejected the token: run `gh auth login` again")
        return resp

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> GitHubResponse:
        headers = await self._headers()
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
        headers = await self._headers()
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

    async def _post(self, path: str, payload: Mapping[str, object]) -> object:
        """A write: never cached or revalidated, and every failure keeps GitHub's cause."""
        resp = await self._send("POST", path, await self._headers(), payload=payload)
        data = _json(resp)
        if not resp.is_success:
            raise GitHubRequestError(resp.status_code, github_error_message(data))
        return data

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
