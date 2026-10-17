# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polite, guarded outbound HTTP for apps (spec §4.5).

Only ``http``/``https``; every hop (including redirects, followed by hand) must resolve to
public addresses only. The transport the module wires in re-checks at connect time, so a
DNS answer that changes between the check and the connection is refused too. Requests to
one host are spaced at least ``min_interval`` apart; responses are revalidated with
ETag / Last-Modified and bodies are capped at ``max_bytes``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from jesinia.services.fetch.application.ports import (
    Article,
    ArticleExtractor,
    CachedFetch,
    FetchCache,
    Resolver,
)
from jesinia.services.fetch.domain.address import is_public

MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})
TIMEOUT = httpx.Timeout(20.0)
USER_AGENT = "Jesinia/0.1 (+personal feed reader)"


# Content encodings we inflate ourselves (bounded); zlib wbits for each.
_ZLIB = {"gzip": 16 + zlib.MAX_WBITS, "x-gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}


class FetchError(RuntimeError):
    """Base for fetch failures callers should report to the user."""


class FetchBlocked(FetchError):  # noqa: N818 - public name fixed by the plan interface
    """The URL is not allowed (scheme, private address, or redirect loop)."""


class FetchTooLarge(FetchError):  # noqa: N818 - public name fixed by the plan interface
    """The body is larger than ``max_bytes``."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    from_cache: bool


async def system_resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to (empty when it does not resolve)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


class FetchService:
    def __init__(
        self,
        cache: FetchCache,
        *,
        resolve: Resolver = system_resolve,
        min_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed: Callable[[str], bool] = is_public,
        extractor: ArticleExtractor | None = None,
    ) -> None:
        """``allowed`` decides which resolved addresses may be fetched; only the
        end-to-end test server widens it (to loopback, for fixture feeds). ``extractor``
        turns a fetched page into an article for ``article``."""
        self._cache = cache
        self._extractor = extractor
        self._allowed = allowed
        self._resolve = resolve
        self._min_interval = min_interval
        self._transport = transport
        self._hosts: dict[str, tuple[asyncio.Lock, list[float]]] = {}

    async def check(self, url: str) -> str:
        """The URL's host, if it may be fetched; ``FetchBlocked`` otherwise."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchBlocked(f"only http(s) URLs can be fetched: {url}")
        host = parts.hostname
        if host == "localhost" or host.endswith(".localhost"):
            raise FetchBlocked(f"{host} is not a public host")
        addresses = [host] if _is_ip(host) else await self._resolve(host)
        if not addresses or not all(self._allowed(a) for a in addresses):
            raise FetchBlocked(f"{host} does not resolve to a public address")
        return host

    async def _pace(self, host: str) -> None:
        lock, last = self._hosts.setdefault(host, (asyncio.Lock(), [0.0]))
        async with lock:
            wait = last[0] + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            last[0] = time.monotonic()

    async def get(self, url: str, *, max_bytes: int = 5_000_000, cache: bool = True) -> FetchResult:
        """GET ``url`` through the guards. ``cache=False`` neither revalidates against nor
        stores in the conditional-request cache (for bodies the caller keeps elsewhere).

        Raises ``FetchBlocked``, ``FetchTooLarge``, or ``FetchError`` (chained to the cause)
        for a transport failure: a timeout, a refused connection, TLS or protocol errors.
        """
        try:
            return await self._get(url, max_bytes=max_bytes, cache=cache)
        except httpx.HTTPError as exc:
            message = str(exc)
            kind = type(exc).__name__
            raise FetchError(f"{kind}: {message}" if message else kind) from exc

    async def _get(self, url: str, *, max_bytes: int, cache: bool) -> FetchResult:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                host = await self.check(url)
                await self._pace(host)
                cached = self._cache.get(url) if cache else None
                headers: dict[str, str] = {}
                if cached is not None and cached.etag:
                    headers["If-None-Match"] = cached.etag
                if cached is not None and cached.last_modified:
                    headers["If-Modified-Since"] = cached.last_modified
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in REDIRECTS and "location" in resp.headers:
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code == 304 and cached is not None:
                        return FetchResult(
                            url, cached.status, cached.content_type, cached.body, True
                        )
                    body = await _read_capped(resp, max_bytes)
                    content_type = resp.headers.get("content-type", "")
                    etag = resp.headers.get("etag")
                    modified = resp.headers.get("last-modified")
                    if cache and resp.status_code == 200 and (etag or modified):
                        self._cache.put(url, CachedFetch(etag, modified, 200, content_type, body))
                    return FetchResult(url, resp.status_code, content_type, body, False)
        raise FetchBlocked(f"too many redirects (more than {MAX_REDIRECTS})")

    async def article(self, url: str) -> Article | None:
        """The main text of the page at ``url``, or None when it has none.

        Raises ``FetchError`` (chained to the original cause) when the page cannot be
        fetched (blocked, too large, a transport error or an HTTP error status), is not
        HTML, cannot be extracted, or no extractor is configured.
        """
        extractor = self._extractor
        if extractor is None:
            raise FetchError("article extraction is not available")
        result = await self.get(url)
        if result.status != 200:
            raise FetchError(f"{url} answered HTTP {result.status}")
        content_type = result.content_type.lower()
        if content_type and "html" not in content_type:
            raise FetchError(f"{url} is not an HTML page ({result.content_type})")
        return await self._extract(extractor, result.body, result.url)

    async def convert(self, html: bytes, url: str) -> Article | None:
        """HTML already in hand (e.g. a feed item's content) as Markdown, through the same
        sanitiser and extractor as ``article``; nothing is fetched. ``url`` resolves
        relative links. Raises ``FetchError`` when no extractor is configured or it fails.
        """
        if self._extractor is None:
            raise FetchError("article extraction is not available")
        return await self._extract(self._extractor, html, url)

    @staticmethod
    async def _extract(extractor: ArticleExtractor, html: bytes, url: str) -> Article | None:
        try:
            # Extraction parses the whole page: keep it off the event loop.
            return await asyncio.to_thread(extractor.extract, html, url)
        # Third-party parser boundary (trafilatura, lxml): whatever it raises is reported
        # as this page's failure, with the original exception chained as the cause.
        except Exception as exc:
            raise FetchError(f"could not extract the article at {url}: {exc}") from exc


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """The body, decompressed with a bound: ``max_bytes`` caps the decoded size, so a
    small compressed response cannot expand past it (httpx would inflate whole chunks)."""
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchTooLarge(f"response is {declared} bytes (limit {max_bytes})")
    encoding = resp.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("identity", "", *_ZLIB):
        raise FetchBlocked(f"unsupported content encoding: {encoding}")
    inflate = zlib.decompressobj(_ZLIB[encoding]) if encoding in _ZLIB else None
    out = bytearray()

    def add(data: bytes) -> None:
        out.extend(data)
        if len(out) > max_bytes:
            raise FetchTooLarge(f"response exceeds {max_bytes} bytes")

    try:
        async for chunk in resp.aiter_raw():
            if inflate is None:
                add(chunk)
                continue
            data = chunk
            while data:
                add(inflate.decompress(data, max_bytes + 1 - len(out)))
                data = inflate.unconsumed_tail
        if inflate is not None:
            add(inflate.flush())
    except zlib.error as exc:  # a server called bytes gzip/deflate that are not
        raise FetchError(f"could not decode the {encoding} body: {exc}") from exc
    return bytes(out)
