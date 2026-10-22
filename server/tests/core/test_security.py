# SPDX-License-Identifier: AGPL-3.0-or-later
import httpx
import pytest
from starlette.types import Receive, Scope, Send

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.security import CSP, RSS_CSP, LocalOriginGuard
from tests.conftest import bearer, lifespan_client


@pytest.fixture
async def client(cfg: Config):  # type: ignore[no-untyped-def]
    async with lifespan_client(create_app(cfg, [])) as c:
        yield c


async def test_rejects_foreign_host(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz", headers={"Host": "evil.example:5374"})).status_code == 421
    assert (await client.get("/healthz", headers={"Host": "127.0.0.1:9999"})).status_code == 421


@pytest.mark.parametrize("host", ["127.0.0.1:5374", "localhost:5374"])
async def test_accepts_local_hosts(client: httpx.AsyncClient, host: str) -> None:
    assert (await client.get("/healthz", headers={"Host": host})).status_code == 200


async def test_rejects_cross_origin_writes(client: httpx.AsyncClient, cfg: Config) -> None:
    h = {**bearer(cfg.token), "Origin": "https://evil.example"}
    assert (await client.post("/api/auth/nonce", headers=h)).status_code == 403
    h_null = {**bearer(cfg.token), "Origin": "null"}
    assert (await client.post("/api/auth/nonce", headers=h_null)).status_code == 403
    same = {**bearer(cfg.token), "Origin": "http://127.0.0.1:5374"}
    assert (await client.post("/api/auth/nonce", headers=same)).status_code == 200
    # reads with a foreign Origin are not state-changing
    assert (
        await client.get("/healthz", headers={"Origin": "https://evil.example"})
    ).status_code == 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.headers["content-security-policy"] == (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-src 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'none'"
    )
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "path", ["/rss/", "/rss/index.html", "/rss", "/rss/sw.js", "/rss/assets/x.js"]
)
async def test_rss_pages_may_show_https_images(client: httpx.AsyncClient, path: str) -> None:
    # Ruling R21: feed images are remote; https only, never http:, and only for the reader.
    csp = (await client.get(path)).headers["content-security-policy"]
    assert "img-src 'self' https:" in csp.split("; ")
    assert "http:" not in csp.replace("https:", "")
    assert "frame-ancestors 'none'" in csp


@pytest.mark.parametrize("path", ["/notes/", "/healthz", "/rssfeed", "/api/rss/feeds"])
async def test_other_pages_keep_the_strict_csp(client: httpx.AsyncClient, path: str) -> None:
    csp = (await client.get(path)).headers["content-security-policy"]
    assert csp == (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-src 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'none'"
    )


async def test_rejections_under_rss_keep_the_strict_csp(client: httpx.AsyncClient) -> None:
    foreign = await client.get("/rss/", headers={"Host": "evil.example:5374"})
    assert foreign.status_code == 421
    assert "img-src" not in foreign.headers["content-security-policy"]
    cross = await client.post("/rss/", headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    assert "img-src" not in cross.headers["content-security-policy"]


async def test_non_http_scopes_pass_through() -> None:
    from jesinia.core.security import LocalOriginGuard

    seen: list[str] = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        seen.append(scope["type"])

    guard = LocalOriginGuard(inner, port=5374)
    await guard({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def _guarded(response_headers: list[tuple[bytes, bytes]]) -> httpx.AsyncClient:
    """A client for a bare ASGI app, behind the guard, that answers with these headers."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": response_headers})
        await send({"type": "http.response.body", "body": b"ok"})

    transport = httpx.ASGITransport(app=LocalOriginGuard(app, port=5374))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5374")


async def test_an_identical_header_a_response_sets_is_sent_once() -> None:
    async with _guarded([(b"X-Content-Type-Options", b"nosniff")]) as c:
        r = await c.get("/api/kb/assets/x/y.png")
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]


async def test_a_response_cannot_weaken_the_security_headers() -> None:
    weak = [
        (b"content-security-policy", b"default-src *; script-src 'unsafe-inline'"),
        (b"X-Frame-Options", b"ALLOWALL"),
        (b"x-content-type-options", b"sniff"),
    ]
    async with _guarded(weak) as c:
        r = await c.get("/notes/")
    assert r.headers.get_list("content-security-policy") == [CSP]
    assert r.headers.get_list("x-frame-options") == ["DENY"]
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]


async def test_rss_pages_keep_their_image_policy_over_a_response_csp() -> None:
    async with _guarded([(b"content-security-policy", b"default-src *")]) as c:
        r = await c.get("/rss/")
    assert r.headers.get_list("content-security-policy") == [RSS_CSP]


REMOTE = "mac.tail1234.ts.net"


@pytest.fixture
async def remote_client(cfg: Config):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    async with lifespan_client(create_app(replace(cfg, remote_host=REMOTE), [])) as c:
        yield c


@pytest.mark.parametrize("host", [REMOTE, f"{REMOTE}:443", "127.0.0.1:5374"])
async def test_should_accept_the_remote_host_when_configured(
    remote_client: httpx.AsyncClient, host: str
) -> None:
    assert (await remote_client.get("/healthz", headers={"Host": host})).status_code == 200


async def test_should_refuse_the_remote_host_when_not_configured(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/healthz", headers={"Host": REMOTE})).status_code == 421


async def test_should_refuse_other_tailnet_hosts(remote_client: httpx.AsyncClient) -> None:
    for host in ("other.tail1234.ts.net", f"{REMOTE}:8443", f"x.{REMOTE}"):
        assert (await remote_client.get("/healthz", headers={"Host": host})).status_code == 421


async def test_should_accept_writes_from_the_remote_https_origin_only(
    remote_client: httpx.AsyncClient, cfg: Config
) -> None:
    def post(origin: str):  # type: ignore[no-untyped-def]
        headers = {**bearer(cfg.token), "Host": REMOTE, "Origin": origin}
        return remote_client.post("/api/auth/nonce", headers=headers)

    assert (await post(f"https://{REMOTE}")).status_code == 200
    assert (await post(f"http://{REMOTE}")).status_code == 403


async def test_should_send_hsts_on_remote_responses_only(
    remote_client: httpx.AsyncClient,
) -> None:
    remote = await remote_client.get("/healthz", headers={"Host": REMOTE})
    assert remote.headers["strict-transport-security"] == "max-age=31536000"
    local = await remote_client.get("/healthz")
    assert "strict-transport-security" not in local.headers
    refused = await remote_client.get("/healthz", headers={"Host": "evil.example"})
    assert "strict-transport-security" not in refused.headers


async def test_health_should_hide_module_details_until_signed_in(
    cfg: Config,
) -> None:
    from dataclasses import replace

    from tests.core.test_app import Exploding

    app = create_app(replace(cfg, remote_host=REMOTE), [Exploding("kb")])
    async with lifespan_client(app) as c:
        anonymous = await c.get("/healthz", headers={"Host": REMOTE})
        assert anonymous.json() == {"ok": False, "modules": {}}
        signed_in = await c.get("/healthz", headers={"Host": REMOTE, **bearer(cfg.token)})
        assert "start failed" in signed_in.json()["modules"]["kb"]["detail"]
        # Host decides nothing: a tailnet client can claim to be local.
        spoofed = await c.get("/healthz", headers={"Host": "127.0.0.1:5374"})
        assert spoofed.json() == {"ok": False, "modules": {}}
