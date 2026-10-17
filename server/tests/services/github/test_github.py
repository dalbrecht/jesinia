# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Scheduler
from jesinia.services.github.application.service import (
    TOKEN_TTL_SECONDS,
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubService,
    GitHubUnavailable,
    UnsafeGitHubPathError,
    WriteMethod,
)
from jesinia.services.github.infrastructure.auth import github_token
from jesinia.services.github.infrastructure.cache import SqliteGitHubCache
from jesinia.services.github.module import GitHubModule
from tests.conftest import lifespan_client

DB = ModuleDb("github", DbClass.CACHE, files("jesinia.services.github.migrations"))


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[SqliteGitHubCache]:
    conn = open_db(tmp_path, DB)
    yield SqliteGitHubCache(conn)
    conn.close()


@pytest.fixture
def gh(cache: SqliteGitHubCache) -> GitHubService:
    async def token() -> str | None:
        return "gho_test"

    return GitHubService(token, cache)


@respx.mock
async def test_etag_reuse(gh: GitHubService) -> None:
    route = respx.get("https://api.github.com/user").mock(
        side_effect=[Response(200, json={"a": 1}, headers={"ETag": "x"}), Response(304)]
    )
    first = await gh.get("/user")
    r = await gh.get("/user")
    assert not first.from_cache and r.from_cache and r.data == {"a": 1} and r.status == 200
    assert route.calls[1].request.headers["If-None-Match"] == "x"
    assert route.calls[0].request.headers["Authorization"] == "Bearer gho_test"


@respx.mock
async def test_params_are_part_of_the_cache_key(gh: GitHubService) -> None:
    respx.get("https://api.github.com/repos/o/r/pulls", params={"state": "open"}).mock(
        return_value=Response(200, json=[1], headers={"ETag": "o"})
    )
    respx.get("https://api.github.com/repos/o/r/pulls", params={"state": "closed"}).mock(
        return_value=Response(200, json=[2])
    )
    assert (await gh.get("/repos/o/r/pulls", {"state": "open"})).data == [1]
    assert (await gh.get("/repos/o/r/pulls", {"state": "closed"})).data == [2]


async def test_unavailable_without_token(cache: SqliteGitHubCache) -> None:
    async def none() -> str | None:
        return None

    gh = GitHubService(none, cache)
    with pytest.raises(GitHubUnavailable):
        await gh.get("/user")
    with pytest.raises(GitHubUnavailable):
        await gh.graphql("{ viewer { login } }", {})


@respx.mock
async def test_rejected_token_and_errors(gh: GitHubService) -> None:
    respx.get("https://api.github.com/user").mock(
        return_value=Response(401, json={"message": "Bad credentials"})
    )
    with pytest.raises(GitHubUnavailable, match="rejected"):
        await gh.get("/user")
    respx.get("https://api.github.com/missing").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    r = await gh.get("/missing")
    assert r.status == 404 and not r.from_cache


@respx.mock
async def test_graphql(gh: GitHubService) -> None:
    respx.post("https://api.github.com/graphql").mock(
        return_value=Response(200, json={"data": {"viewer": {"login": "me"}}})
    )
    assert await gh.graphql("{ viewer { login } }", {}) == {"viewer": {"login": "me"}}
    respx.post("https://api.github.com/graphql").mock(
        return_value=Response(200, json={"errors": [{"message": "bad"}]})
    )
    with pytest.raises(RuntimeError, match="bad"):
        await gh.graphql("{ x }", {})


def test_prune_old_rows(cache: SqliteGitHubCache) -> None:
    cache.put("k1", "e", 200, {"x": 1})
    cache.conn.execute(
        "UPDATE responses SET fetched_at = ?",
        ((datetime.now(UTC) - timedelta(days=31)).isoformat(),),
    )
    cache.put("k2", "e", 200, {"x": 2})
    assert (
        cache.prune(timedelta(days=30)) == 1
        and cache.get("k1") is None
        and cache.get("k2") is not None
    )


async def test_github_token_sources() -> None:
    async def gh_ok(argv: list[str]) -> tuple[int, str]:
        return (0, "gho_from_gh\n") if argv[0].endswith("gh") else (1, "")

    assert await github_token(run=gh_ok, which=lambda b: "/bin/" + b) == "gho_from_gh"

    calls: list[list[str]] = []

    async def keychain(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return (1, "") if argv[0].endswith("gh") else (0, "pat_from_keychain\n")

    assert await github_token(run=keychain, which=lambda b: "/bin/" + b) == "pat_from_keychain"
    assert calls[1] == ["/bin/security", "find-generic-password", "-s", "jesinia-github", "-w"]
    assert await github_token(run=keychain, which=lambda b: None) is None

    async def nothing(argv: list[str]) -> tuple[int, str]:
        return 1, ""

    assert await github_token(run=nothing, which=lambda b: "/bin/" + b) is None


async def test_real_runner_capture() -> None:
    from jesinia.services.github.infrastructure.auth import _run

    code, out = await _run(["python3", "-c", "print('x')"])
    assert code == 0 and out.strip() == "x"
    assert (await _run(["/nonexistent/binary"]))[0] != 0


async def test_module_lifecycle(cfg: Config) -> None:
    async def tok() -> str | None:
        return None

    mod = GitHubModule(token=tok)
    assert mod.health().ok is False and mod.router() is None and list(mod.mcp_tools()) == []
    async with lifespan_client(create_app(cfg, [mod])) as c:
        h = (await c.get("/healthz")).json()["modules"]["github"]
        assert h["ok"] is True and "gh auth login" in h["detail"]
    await mod.stop()

    async def yes() -> str | None:
        return "t"

    mod2 = GitHubModule(token=yes)
    async with lifespan_client(create_app(cfg, [mod2])) as c:
        assert (await c.get("/healthz")).json()["modules"]["github"]["detail"] == "token available"


@respx.mock
async def test_non_json_body_and_graphql_rejected_token(gh: GitHubService) -> None:
    respx.get("https://api.github.com/raw").mock(return_value=Response(200, text="plain"))
    assert (await gh.get("/raw")).data == "plain"
    respx.post("https://api.github.com/graphql").mock(return_value=Response(401, json={}))
    with pytest.raises(GitHubUnavailable):
        await gh.graphql("{ x }", {})


@respx.mock
async def test_transport_errors_are_typed_and_keep_their_cause(gh: GitHubService) -> None:
    lost = httpx.ConnectError("connection refused")
    respx.get("https://api.github.com/user").mock(side_effect=lost)
    with pytest.raises(GitHubRequestError, match="connection refused") as caught:
        await gh.get("/user")
    assert caught.value.status is None and caught.value.__cause__ is lost
    timeout = httpx.ReadTimeout("read timed out")
    respx.post("https://api.github.com/graphql").mock(side_effect=timeout)
    with pytest.raises(GitHubRequestError, match="read timed out") as caught:
        await gh.graphql("{ x }", {})
    assert caught.value.__cause__ is timeout


@respx.mock
async def test_graphql_server_errors_are_typed(gh: GitHubService) -> None:
    respx.post("https://api.github.com/graphql").mock(
        return_value=Response(502, text="<html>Bad gateway</html>")
    )
    with pytest.raises(GitHubRequestError, match="Bad gateway") as caught:
        await gh.graphql("{ x }", {})
    assert caught.value.status == 502
    respx.post("https://api.github.com/graphql").mock(
        return_value=Response(500, json={"message": "Something went wrong"})
    )
    with pytest.raises(GitHubRequestError, match="500: Something went wrong"):
        await gh.graphql("{ x }", {})
    respx.post("https://api.github.com/graphql").mock(return_value=Response(200, json={}))
    assert await gh.graphql("{ x }", {}) == {}


class _Tokens:
    """A token source that counts lookups and hands out a new token each time."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        return f"gho_secret_{self.calls}"


@respx.mock
async def test_token_is_looked_up_once_per_five_minutes(cache: SqliteGitHubCache) -> None:
    tokens = _Tokens()
    now = [1000.0]
    gh = GitHubService(tokens, cache, clock=lambda: now[0])
    route = respx.get("https://api.github.com/user").mock(return_value=Response(200, json={}))
    await asyncio.gather(*(gh.get("/user") for _ in range(10)))
    for _ in range(10):
        await gh.get("/user")
    assert tokens.calls == 1 and route.call_count == 20
    now[0] += TOKEN_TTL_SECONDS - 1
    await gh.get("/user")
    assert tokens.calls == 1
    now[0] += 2  # past five minutes since the lookup
    await gh.get("/user")
    assert tokens.calls == 2
    assert route.calls[-1].request.headers["Authorization"] == "Bearer gho_secret_2"
    await gh.aclose()


@respx.mock
async def test_a_401_refreshes_the_token_and_retries_once(cache: SqliteGitHubCache) -> None:
    tokens = _Tokens()
    gh = GitHubService(tokens, cache)
    route = respx.get("https://api.github.com/user").mock(
        side_effect=[Response(401, json={"message": "Bad credentials"}), Response(200, json={})]
    )
    assert (await gh.get("/user")).status == 200
    assert tokens.calls == 2 and route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer gho_secret_2"
    await gh.aclose()


@respx.mock
async def test_a_second_401_is_typed_and_never_shows_the_token(
    cache: SqliteGitHubCache,
) -> None:
    tokens = _Tokens()
    gh = GitHubService(tokens, cache)
    route = respx.post("https://api.github.com/graphql").mock(
        return_value=Response(401, json={"message": "Bad credentials"})
    )
    with pytest.raises(GitHubUnavailable) as caught:
        await gh.graphql("{ viewer { login } }", {})
    assert tokens.calls == 2 and route.call_count == 2
    assert "gho_secret" not in str(caught.value) and "gho_secret" not in repr(caught.value)
    await gh.aclose()


@respx.mock
async def test_one_http_client_serves_every_request(
    cache: SqliteGitHubCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    def counting(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real(*args, **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr("jesinia.services.github.application.service.httpx.AsyncClient", counting)
    gh = GitHubService(_Tokens(), cache)
    respx.get("https://api.github.com/user").mock(return_value=Response(200, json={}))
    respx.post("https://api.github.com/graphql").mock(return_value=Response(200, json={"data": {}}))
    await gh.get("/user")
    await gh.graphql("{ x }", {})
    await gh.get("/user")
    assert len(made) == 1
    await gh.aclose()
    assert gh.closed and made[0].is_closed
    await gh.aclose()  # idempotent


@respx.mock
async def test_the_client_is_closed_when_the_module_stops(cfg: Config) -> None:
    mod = GitHubModule(token=_Tokens())
    ctx = AppContext(cfg, EventBus(), Scheduler())
    await mod.start(ctx)
    service = ctx.services["github"]
    assert isinstance(service, GitHubService)
    respx.get("https://api.github.com/user").mock(return_value=Response(200, json={}))
    await service.get("/user")
    assert not service.closed
    await mod.stop()
    assert service.closed


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _fixed_wall_clock() -> datetime:
    return NOW


@respx.mock
async def test_a_429_is_rate_limited_until_retry_after(cache: SqliteGitHubCache) -> None:
    async def token() -> str | None:
        return "gho_test"

    gh = GitHubService(token, cache, wall_clock=_fixed_wall_clock)
    respx.get("https://api.github.com/user").mock(
        return_value=Response(
            429, headers={"retry-after": "90"}, json={"message": "secondary rate limit"}
        )
    )
    with pytest.raises(GitHubRateLimitedError, match="secondary rate limit") as caught:
        await gh.get("/user")
    assert caught.value.status == 429
    assert caught.value.reset_at == NOW + timedelta(seconds=90)
    assert "12:01:30" in str(caught.value)


@respx.mock
async def test_a_403_without_remaining_quota_carries_the_reset_time(gh: GitHubService) -> None:
    reset = datetime(2026, 10, 16, 13, 0, tzinfo=UTC)
    respx.get("https://api.github.com/user").mock(
        return_value=Response(
            403,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(reset.timestamp())),
            },
            json={"message": "API rate limit exceeded"},
        )
    )
    with pytest.raises(GitHubRateLimitedError) as caught:
        await gh.get("/user")
    assert isinstance(caught.value, GitHubRequestError)
    assert caught.value.reset_at == reset and caught.value.status == 403


@respx.mock
async def test_a_rate_limit_without_a_reset_time_still_is_typed(gh: GitHubService) -> None:
    respx.post("https://api.github.com/graphql").mock(
        return_value=Response(429, headers={"retry-after": "soon"}, text="slow down")
    )
    with pytest.raises(GitHubRateLimitedError, match="slow down") as caught:
        await gh.graphql("{ x }", {})
    assert caught.value.reset_at is None


@respx.mock
async def test_a_plain_403_is_not_a_rate_limit(gh: GitHubService) -> None:
    respx.get("https://api.github.com/secret").mock(
        return_value=Response(
            403, headers={"x-ratelimit-remaining": "4999"}, json={"message": "Forbidden"}
        )
    )
    response = await gh.get("/secret")
    assert response.status == 403


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
@respx.mock
async def test_every_write_method_is_rate_limited_like_a_post(
    cache: SqliteGitHubCache, method: WriteMethod
) -> None:
    async def token() -> str | None:
        return "gho_test"

    gh = GitHubService(token, cache, wall_clock=_fixed_wall_clock)
    respx.route(method=method, url="https://api.github.com/repos/o/r/issues/1").mock(
        return_value=Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793534400"},
            json={"message": "API rate limit exceeded"},
        )
    )
    with pytest.raises(GitHubRateLimitedError, match="rate limit exceeded") as caught:
        await gh.write(method, "/repos/o/r/issues/1", {"state": "closed"})
    assert caught.value.reset_at == datetime.fromtimestamp(1793534400, UTC)
    await gh.aclose()


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
@respx.mock
async def test_every_write_method_retries_once_on_a_401_with_a_fresh_token(
    cache: SqliteGitHubCache, method: WriteMethod
) -> None:
    tokens = _Tokens()
    gh = GitHubService(tokens, cache)
    route = respx.route(method=method, url="https://api.github.com/repos/o/r/issues/1").mock(
        side_effect=[Response(401, json={"message": "Bad credentials"}), Response(200, json={})]
    )
    assert await gh.write(method, "/repos/o/r/issues/1") == {}
    assert tokens.calls == 2 and route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer gho_secret_2"
    route.side_effect = [Response(401, json={}), Response(401, json={})]
    with pytest.raises(GitHubUnavailable):
        await gh.write(method, "/repos/o/r/issues/1")
    assert route.call_count == 4  # one retry, never a loop
    await gh.aclose()


@respx.mock
async def test_paths_that_normalise_elsewhere_are_refused_before_sending(
    gh: GitHubService,
) -> None:
    anything = respx.route(host="api.github.com").mock(return_value=Response(200, json={}))
    for path in ("/repos/o/r/labels/..", "/repos/o/r/labels/.", "/repos/o/r/x/../y", "/a b"):
        with pytest.raises(UnsafeGitHubPathError):
            await gh.write("DELETE", path)
        with pytest.raises(UnsafeGitHubPathError):
            await gh.get(path)
    with pytest.raises(UnsafeGitHubPathError, match="/repos/"):
        await gh.write("POST", "/user/repos", {"name": "x"})
    assert not anything.called
