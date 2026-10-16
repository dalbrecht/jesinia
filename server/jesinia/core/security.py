# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local-origin guard and security headers (spec §7.3).

- ``421`` when ``Host`` is not ``127.0.0.1:<port>`` or ``localhost:<port>``
  (DNS-rebinding defense).
- ``403`` for state-changing requests whose ``Origin`` is present and not the local origin.
- A strict CSP (including ``frame-ancestors 'none'``: no other local port may frame the
  authenticated UI) plus ``X-Frame-Options``, ``nosniff``, and ``no-referrer``.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
    "object-src 'none'; base-uri 'none'"
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),  # legacy equivalent of frame-ancestors 'none'
]


class LocalOriginGuard:
    def __init__(self, app: ASGIApp, port: int) -> None:
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    async def _reject(self, send: Send, code: int, text: str) -> None:
        body = text.encode()
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), *_HEADERS],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("host", "") not in self.hosts:
            await self._reject(send, 421, "misdirected request")
            return
        origin = headers.get("origin")
        if (
            scope["method"] not in SAFE_METHODS
            and origin is not None
            and origin not in self.origins
        ):
            await self._reject(send, 403, "cross-origin request refused")
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *_HEADERS]
            await send(message)

        await self.app(scope, receive, send_with_headers)
