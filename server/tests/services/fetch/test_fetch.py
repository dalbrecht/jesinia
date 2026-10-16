# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import time
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
import respx
from httpx import Response

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.services.fetch.application.service import FetchBlocked, FetchService, FetchTooLarge
from jesinia.services.fetch.domain.address import is_public
from jesinia.services.fetch.infrastructure.cache import SqliteFetchCache
from jesinia.services.fetch.infrastructure.sanitize import sanitize_html
from jesinia.services.fetch.infrastructure.transport import GuardedBackend, guarded_transport
from jesinia.services.fetch.module import FetchModule
from tests.conftest import lifespan_client

DB = ModuleDb("fetch", DbClass.CACHE, files("jesinia.services.fetch.migrations"))
PUBLIC = "93.184.216.34"


def resolver(mapping: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    async def resolve(host: str) -> list[str]:
        return mapping.get(host, [PUBLIC])

    return resolve


@pytest.fixture
def fetch(tmp_path: Path) -> Iterator[FetchService]:
    conn = open_db(tmp_path, DB)
    yield FetchService(
        SqliteFetchCache(conn), resolve=resolver({"internal.example": ["10.0.0.5"]}), min_interval=0
    )
    conn.close()


async def test_fetch_blocks_private_addresses(fetch: FetchService) -> None:
    for url in (
        "http://127.0.0.1:4870/api",
        "http://localhost/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data",
        "http://internal.example/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ):
        with pytest.raises(FetchBlocked):
            await fetch.get(url)


@respx.mock
async def test_redirect_to_private_is_blocked(fetch: FetchService) -> None:
    respx.get("https://pub.example/a").mock(
        return_value=Response(302, headers={"Location": "http://internal.example/secret"})
    )
    with pytest.raises(FetchBlocked):
        await fetch.get("https://pub.example/a")


@respx.mock
async def test_follows_public_redirect_and_caches_conditionally(fetch: FetchService) -> None:
    respx.get("https://pub.example/a").mock(return_value=Response(301, headers={"Location": "/b"}))
    route = respx.get("https://pub.example/b").mock(
        side_effect=[
            Response(
                200,
                text="<p>hi</p>",
                headers={
                    "ETag": "v1",
                    "Last-Modified": "Wed, 01 Oct 2026 00:00:00 GMT",
                    "Content-Type": "text/html",
                },
            ),
            Response(304),
        ]
    )
    first = await fetch.get("https://pub.example/a")
    assert (
        first.url == "https://pub.example/b" and first.body == b"<p>hi</p>" and not first.from_cache
    )
    second = await fetch.get("https://pub.example/b")
    assert second.from_cache and second.body == b"<p>hi</p>" and second.content_type == "text/html"
    assert route.calls[1].request.headers["If-None-Match"] == "v1"
    assert route.calls[1].request.headers["If-Modified-Since"].startswith("Wed")


@respx.mock
async def test_fetch_size_cap(fetch: FetchService) -> None:
    respx.get("https://pub.example/big").mock(return_value=Response(200, content=b"x" * 2000))
    with pytest.raises(FetchTooLarge):
        await fetch.get("https://pub.example/big", max_bytes=1000)
    respx.get("https://pub.example/declared").mock(
        return_value=Response(200, content=b"x", headers={"Content-Length": "999999"})
    )
    with pytest.raises(FetchTooLarge):
        await fetch.get("https://pub.example/declared", max_bytes=1000)


@respx.mock
async def test_too_many_redirects(fetch: FetchService) -> None:
    respx.get("https://pub.example/loop").mock(
        return_value=Response(302, headers={"Location": "/loop"})
    )
    with pytest.raises(FetchBlocked, match="redirects"):
        await fetch.get("https://pub.example/loop")


@respx.mock
async def test_per_host_minimum_interval(tmp_path: Path) -> None:
    conn = open_db(tmp_path, DB)
    svc = FetchService(SqliteFetchCache(conn), resolve=resolver({}), min_interval=0.3)
    respx.get("https://slow.example/1").mock(return_value=Response(200, text="1"))
    respx.get("https://slow.example/2").mock(return_value=Response(200, text="2"))
    start = time.monotonic()
    await asyncio.gather(svc.get("https://slow.example/1"), svc.get("https://slow.example/2"))
    assert time.monotonic() - start >= 0.3
    conn.close()


async def test_default_resolver_blocks_unresolvable(tmp_path: Path) -> None:
    conn = open_db(tmp_path, DB)
    svc = FetchService(SqliteFetchCache(conn))
    with pytest.raises(FetchBlocked):
        await svc.get("http://no-such-host.invalid/x")
    with pytest.raises(FetchBlocked):
        await svc.get("http://localhost:4870/")
    conn.close()


def test_sanitize_strips_script_and_handlers() -> None:
    out = sanitize_html(
        '<p onclick="x()">a</p><script>x()</script><a href="/b">l</a>', "https://s.example/p"
    )
    assert "script" not in out and "onclick" not in out and 'href="https://s.example/b"' in out
    assert 'rel="noopener noreferrer"' in out


def test_sanitize_allowlist_and_urls() -> None:
    html = (
        '<h2>T</h2><img src="i.png" alt="pic" onerror="x"><iframe src="//evil"></iframe>'
        '<a href="javascript:alert(1)">bad</a><table><tr><td>c</td></tr></table>'
        "<style>p{}</style><pre><code>x</code></pre>"
    )
    out = sanitize_html(html, "https://s.example/dir/page")
    assert 'src="https://s.example/dir/i.png"' in out and "onerror" not in out
    assert "iframe" not in out and "javascript:" not in out and "<style>" not in out
    assert "<table>" in out and "<h2>" in out and "<code>" in out


def test_is_public() -> None:
    assert is_public("93.184.216.34") and is_public("2606:2800:220:1::1")
    for a in (
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1%en0",
        "fc00::1",
        "224.0.0.1",
        "::ffff:127.0.0.1",
        "not-an-ip",
    ):
        assert not is_public(a), a


async def test_guarded_backend_refuses_private_at_connect_time() -> None:
    backend = GuardedBackend(resolver({"rebind.example": ["127.0.0.1"]}))
    with pytest.raises(FetchBlocked):
        await backend.connect_tcp("rebind.example", 80)
    with pytest.raises(FetchBlocked):
        await backend.connect_unix_socket("/var/run/docker.sock")
    await backend.sleep(0)


async def test_guarded_transport_connects_to_vetted_address(tmp_path: Path) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    conn = open_db(tmp_path, DB)
    resolve = resolver({"site.example": ["127.0.0.1"]})
    svc = FetchService(
        SqliteFetchCache(conn),
        resolve=resolver({"site.example": [PUBLIC]}),
        min_interval=0,
        transport=guarded_transport(resolve, allowed=lambda a: True),
    )
    try:
        r = await svc.get(f"http://site.example:{port}/x")
        assert r.status == 200 and r.body == b"ok" and r.content_type == "text/plain"
        strict = FetchService(
            SqliteFetchCache(conn),
            resolve=resolver({"site.example": [PUBLIC]}),
            min_interval=0,
            transport=guarded_transport(resolve),
        )
        with pytest.raises(FetchBlocked):
            await strict.get(f"http://site.example:{port}/x")
    finally:
        server.close()
        await server.wait_closed()
        conn.close()


async def test_module_lifecycle(cfg: Config) -> None:
    mod = FetchModule()
    assert mod.health().ok is False and mod.router() is None and list(mod.mcp_tools()) == []
    async with lifespan_client(create_app(cfg, [mod])) as c:
        assert (await c.get("/healthz")).json()["modules"]["fetch"] == {
            "ok": True,
            "detail": "ready",
        }
    assert mod.health().ok is False
