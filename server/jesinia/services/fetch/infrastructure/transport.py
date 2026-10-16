# SPDX-License-Identifier: AGPL-3.0-or-later
"""An httpx transport that only opens TCP connections to public addresses.

The service checks each URL before requesting it; this backend resolves again at connect
time and connects to the vetted address itself, closing the DNS-rebinding gap between
the two. TLS still verifies against the URL's hostname.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import httpcore
import httpx

from jesinia.services.fetch.application.ports import Resolver
from jesinia.services.fetch.application.service import FetchBlocked
from jesinia.services.fetch.domain.address import is_public


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolve: Resolver, allowed: Callable[[str], bool] = is_public) -> None:
        self._resolve = resolve
        self._allowed = allowed
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolve(host)
        if not addresses or not all(self._allowed(a) for a in addresses):
            raise FetchBlocked(f"{host} does not resolve to a public address")
        return await self._inner.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise FetchBlocked("unix sockets are never fetched")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(
    resolve: Resolver, allowed: Callable[[str], bool] = is_public
) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        network_backend=GuardedBackend(resolve, allowed),
    )
    return transport
