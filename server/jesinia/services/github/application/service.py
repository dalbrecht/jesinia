# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub REST/GraphQL access with ETag revalidation (spec §4.5).

The token comes from ``gh`` or the Keychain on every call and is never stored. A 304
answer is served from ``github.sqlite`` (304s do not count against the rate limit).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from jesinia.services.github.application.ports import GitHubCache, TokenSource

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(20.0)


class GitHubError(RuntimeError):
    """Base for GitHub service failures."""


class GitHubUnavailable(GitHubError):  # noqa: N818 - public name fixed by the plan interface
    """No usable token: GitHub features are off until ``gh auth login`` (or a Keychain PAT)."""


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

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> GitHubResponse:
        headers = await self._headers()
        query = urlencode(sorted((params or {}).items()))
        key = f"{path}?{query}" if query else path
        cached = self._cache.get(key)
        if cached is not None:
            headers["If-None-Match"] = cached.etag
        async with self._client() as client:
            resp = await client.get(path, params=params, headers=headers)
        if resp.status_code == 401:
            raise GitHubUnavailable("GitHub rejected the token: run `gh auth login` again")
        if resp.status_code == 304 and cached is not None:
            return GitHubResponse(cached.status, cached.data, True)
        data = _json(resp)
        etag = resp.headers.get("ETag")
        if resp.is_success and etag:
            self._cache.put(key, etag, resp.status_code, data)
        return GitHubResponse(resp.status_code, data, False)

    async def graphql(self, query: str, variables: Mapping[str, object]) -> dict[str, object]:
        headers = await self._headers()
        async with self._client() as client:
            resp = await client.post(
                "/graphql", json={"query": query, "variables": dict(variables)}, headers=headers
            )
        if resp.status_code == 401:
            raise GitHubUnavailable("GitHub rejected the token: run `gh auth login` again")
        body = resp.json()
        if errors := body.get("errors"):
            raise RuntimeError("; ".join(str(e.get("message", e)) for e in errors))
        resp.raise_for_status()
        data: dict[str, object] = body.get("data") or {}
        return data
