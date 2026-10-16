# SPDX-License-Identifier: AGPL-3.0-or-later
import httpx
import pytest

from jesinia.core.app import create_app
from jesinia.core.config import Config
from tests.conftest import bearer, lifespan_client


@pytest.fixture
async def client(cfg: Config):  # type: ignore[no-untyped-def]
    async with lifespan_client(create_app(cfg, [])) as c:
        yield c


async def test_rejects_foreign_host(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz", headers={"Host": "evil.example:4870"})).status_code == 421
    assert (await client.get("/healthz", headers={"Host": "127.0.0.1:9999"})).status_code == 421


@pytest.mark.parametrize("host", ["127.0.0.1:4870", "localhost:4870"])
async def test_accepts_local_hosts(client: httpx.AsyncClient, host: str) -> None:
    assert (await client.get("/healthz", headers={"Host": host})).status_code == 200


async def test_rejects_cross_origin_writes(client: httpx.AsyncClient, cfg: Config) -> None:
    h = {**bearer(cfg.token), "Origin": "https://evil.example"}
    assert (await client.post("/api/auth/nonce", headers=h)).status_code == 403
    h_null = {**bearer(cfg.token), "Origin": "null"}
    assert (await client.post("/api/auth/nonce", headers=h_null)).status_code == 403
    same = {**bearer(cfg.token), "Origin": "http://127.0.0.1:4870"}
    assert (await client.post("/api/auth/nonce", headers=same)).status_code == 200
    # reads with a foreign Origin are not state-changing
    assert (
        await client.get("/healthz", headers={"Origin": "https://evil.example"})
    ).status_code == 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.headers["content-security-policy"] == (
        "default-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'none'"
    )
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_non_http_scopes_pass_through() -> None:
    from jesinia.core.security import LocalOriginGuard

    seen: list[str] = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        seen.append(scope["type"])

    guard = LocalOriginGuard(inner, port=4870)
    await guard({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]
