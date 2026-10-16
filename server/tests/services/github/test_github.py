# SPDX-License-Identifier: AGPL-3.0-or-later
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
import respx
from httpx import Response

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.services.github.application.service import GitHubService, GitHubUnavailable
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
