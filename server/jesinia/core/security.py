# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local-origin guard and security headers (spec §7.3).

- ``421`` when ``Host`` is not ``127.0.0.1:<port>`` or ``localhost:<port>``, or the
  configured tailnet host (ADR 0011) (DNS-rebinding defense).
- ``403`` for state-changing requests whose ``Origin`` is present and not the local origin
  (or ``https://<remote host>``).
- HSTS on responses to the remote host, which ``tailscale serve`` only offers over HTTPS.
- A strict CSP (including ``frame-ancestors 'none'``: no other local port may frame the
  authenticated UI; the RSS pages may also load https images) plus ``X-Frame-Options``,
  ``nosniff``, and ``no-referrer``.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Scripts, fonts and connections stay same-origin. Inline *styles* are allowed: the
# editor (CodeMirror) injects its theme at runtime, and the static PWAs cannot carry a
# per-response nonce.
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-src 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; base-uri 'none'"
)
# Ruling R21: the RSS reader shows feed images, which live on other hosts. Its pages alone
# may load images over https (never http:); with ``no-referrer`` the hosts learn nothing of
# the reader. Scripts, frames and connections stay as strict as everywhere else.
RSS_CSP = f"{CSP}; img-src 'self' https:"
RSS_PREFIX = "/rss"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_OTHER_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),  # legacy equivalent of frame-ancestors 'none'
]
_HSTS = (b"strict-transport-security", b"max-age=31536000")
_HEADERS = [(b"content-security-policy", CSP.encode()), *_OTHER_HEADERS]
_RSS_HEADERS = [(b"content-security-policy", RSS_CSP.encode()), *_OTHER_HEADERS]


def _headers_for(path: str) -> list[tuple[bytes, bytes]]:
    """The RSS PWA's pages (``/rss``, ``/rss/…``) get the image allowance; nothing else does."""
    is_rss_page = path == RSS_PREFIX or path.startswith(f"{RSS_PREFIX}/")
    return _RSS_HEADERS if is_rss_page else _HEADERS


def _enforce(
    headers: list[tuple[bytes, bytes]], policy: list[tuple[bytes, bytes]]
) -> list[tuple[bytes, bytes]]:
    """``headers`` with the security ``policy`` applied: the policy's value always wins. A
    response's own copy of a policy header (an asset's ``nosniff``) is kept only when it is
    the same value, so it is sent once; any other value (a weaker CSP, say) is dropped."""
    wanted = {name: value for name, value in policy}
    kept = [
        (name, value)
        for name, value in headers
        if name.lower() not in wanted or value == wanted[name.lower()]
    ]
    present = {(name.lower(), value) for name, value in kept}
    return [*kept, *(h for h in policy if h not in present)]


def is_remote_host(host: str, remote_host: str | None) -> bool:
    """Whether a request's ``Host`` header names the tailnet host (with or without :443)."""
    return remote_host is not None and host in (remote_host, f"{remote_host}:443")


class LocalOriginGuard:
    def __init__(self, app: ASGIApp, port: int, remote_host: str | None = None) -> None:
        self.app = app
        self.remote_host = remote_host
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        if remote_host is not None:
            self.hosts |= {remote_host, f"{remote_host}:443"}
            self.origins.add(f"https://{remote_host}")

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

        extra = _headers_for(scope["path"])
        if is_remote_host(headers["host"], self.remote_host):
            extra = [*extra, _HSTS]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = _enforce(list(message.get("headers", [])), extra)
            await send(message)

        await self.app(scope, receive, send_with_headers)
