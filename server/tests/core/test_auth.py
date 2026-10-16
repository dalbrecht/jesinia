# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import timedelta

import httpx
import pytest

from jesinia.core import auth
from jesinia.core.app import create_app
from jesinia.core.auth import issue_login_nonce, register_session_token
from jesinia.core.config import Config
from tests.conftest import BASE_URL, bearer, lifespan_client


@pytest.fixture
async def client(cfg: Config):  # type: ignore[no-untyped-def]
    async with lifespan_client(create_app(cfg, [])) as c:
        yield c


async def test_api_requires_auth(client: httpx.AsyncClient, cfg: Config) -> None:
    assert (await client.get("/api/ping")).status_code == 401
    assert (await client.get("/api/ping", headers=bearer(cfg.token))).status_code == 200
    assert (await client.get("/api/ping", headers=bearer("wrong"))).status_code == 401
    assert (await client.get("/api/ping", headers={"Authorization": "Basic x"})).status_code == 401


async def test_nonce_login_sets_cookie_once(client: httpx.AsyncClient) -> None:
    n = issue_login_nonce()
    r = await client.get(f"/login?nonce={n}", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "jesinia_session" in r.cookies and "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/" in cookie
    assert (await client.get(f"/login?nonce={n}")).status_code == 401


async def test_cookie_authenticates_api(client: httpx.AsyncClient) -> None:
    await client.get(f"/login?nonce={issue_login_nonce()}", follow_redirects=False)
    assert "jesinia_session" in client.cookies  # the client keeps the cookie
    ok = await client.get("/api/ping")
    assert ok.status_code == 200 and ok.json() == {"ok": True, "principal": "user"}
    auth.invalidate_sessions()
    assert (await client.get("/api/ping")).status_code == 401


async def test_expired_nonce_rejected(client: httpx.AsyncClient) -> None:
    n = issue_login_nonce(ttl=timedelta(seconds=-1))
    assert (await client.get(f"/login?nonce={n}")).status_code == 401
    assert (await client.get("/login")).status_code in (401, 422)


async def test_session_token_principal(client: httpx.AsyncClient) -> None:
    register_session_token("sess-token", "s-1")
    r = await client.get("/api/ping", headers=bearer("sess-token"))
    assert r.json() == {"ok": True, "principal": "session", "session_id": "s-1"}
    auth.revoke_session_token("sess-token")
    assert (await client.get("/api/ping", headers=bearer("sess-token"))).status_code == 401


async def test_nonce_endpoint_issues_login_url(client: httpx.AsyncClient, cfg: Config) -> None:
    assert (await client.post("/api/auth/nonce")).status_code == 401
    body = (await client.post("/api/auth/nonce", headers=bearer(cfg.token))).json()
    assert body["url"] == f"{BASE_URL}/login?nonce={body['nonce']}"
    assert (
        await client.get(body["url"].removeprefix(BASE_URL), follow_redirects=False)
    ).status_code in (302, 303)


async def test_session_principal_cannot_mint_nonces(client: httpx.AsyncClient) -> None:
    register_session_token("agent", "s-2")
    assert (await client.post("/api/auth/nonce", headers=bearer("agent"))).status_code == 403


async def test_reload_token_adopts_rotated_token_and_signs_out(
    client: httpx.AsyncClient, cfg: Config
) -> None:
    from dataclasses import replace

    from jesinia.core.config import save_config

    nonce = issue_login_nonce()
    await client.get(f"/login?nonce={nonce}")
    assert (await client.get("/api/ping")).status_code == 200  # cookie session
    save_config(replace(cfg, token="n" * 43), cfg.data_dir / "config.toml")
    r = await client.post("/api/auth/reload-token", headers=bearer(cfg.token))
    assert r.status_code == 200
    assert (await client.get("/api/ping")).status_code == 401  # cookie signed out
    assert (await client.get("/api/ping", headers=bearer(cfg.token))).status_code == 401
    assert (await client.get("/api/ping", headers=bearer("n" * 43))).status_code == 200
