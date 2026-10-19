# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication (spec §7.4).

- PWAs: a short-lived, single-use login nonce (printed by ``./setup`` / ``jesinia status``,
  opened by ``jesinia login``) is exchanged at ``/login`` for an ``HttpOnly; SameSite=Strict``
  session cookie. The install token never appears in a URL.
- MCP clients and the CLI: ``Authorization: Bearer <install token>``.
- Agent sessions launched by Jesinia: per-session bearer tokens (plan Task 18).

Browser sessions last 30 days and renew (at most daily) while in use. They survive restarts
(ADR 0010): ``sessions.json`` in the data dir, mode 600, holds only SHA-256 hashes of the
cookie values, so the file alone cannot sign anyone in. It also holds a fingerprint of the
install token; a file saved under another token is discarded, so rotating the token (even
with the server down) always signs every browser out. Saving it is best-effort; a failed
save keeps the session working until the next restart. Everything else is process-local:
the server is a single process (spec §6).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jesinia.core.config import Config, load_config, save_config

COOKIE = "jesinia_session"
DEFAULT_NONCE_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)
# Renewing rewrites sessions.json and re-sends the cookie; once a day is plenty.
SESSION_RENEW_AFTER = timedelta(days=1)
SESSIONS_FILE = "sessions.json"
_RENEW_STATE_KEY = "jesinia_renewed_session"

log = logging.getLogger(__name__)
_nonces: dict[str, datetime] = {}
_cookie_sessions: dict[str, datetime] = {}  # SHA-256 of the cookie value → expiry
_sessions_path: Path | None = None
_token_fingerprint = ""
# require_user is a sync dependency, so it runs on FastAPI's threadpool while rotation runs
# on the event loop: every change to the sessions and every save happens under this lock.
_sessions_lock = threading.Lock()
_session_tokens: dict[str, str] = {}


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "session"]
    session_id: str | None = None


def reset_state() -> None:
    """Forget all in-memory auth state and stop persisting sessions (tests)."""
    global _sessions_path, _token_fingerprint
    _nonces.clear()
    _session_tokens.clear()
    with _sessions_lock:
        _cookie_sessions.clear()
        _sessions_path = None
        _token_fingerprint = ""


def invalidate_sessions(new_token: str | None = None) -> None:
    """Log out every browser (``jesinia token rotate``), on disk too. With ``new_token``,
    the saved file is bound to it, so even if this save fails the old file is discarded
    at the next start (it carries the old token's fingerprint)."""
    global _token_fingerprint
    with _sessions_lock:
        _cookie_sessions.clear()
        if new_token is not None:
            _token_fingerprint = _fingerprint(new_token)
        _save_sessions()


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _fingerprint(token: str) -> str:
    return hashlib.sha256(b"jesinia-sessions:" + token.encode()).hexdigest()


def load_sessions(path: Path, token: str) -> None:
    """Adopt the browser sessions saved at ``path`` under ``token`` and save future changes
    there. A missing or unreadable file, or one saved under another token, starts with no
    sessions: everyone signs in again, nothing breaks."""
    global _sessions_path, _token_fingerprint
    with _sessions_lock:
        _sessions_path = path
        _token_fingerprint = _fingerprint(token)
        _cookie_sessions.clear()
        if not path.exists():
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("token") != _token_fingerprint:
                log.info("browser sessions at %s predate a token rotation; discarded", path)
                return
            now = _now()
            for digest, expires_at in saved["sessions"].items():
                expires = datetime.fromisoformat(expires_at)
                if expires > now:
                    _cookie_sessions[str(digest)] = expires
        except (OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
            _cookie_sessions.clear()
            log.warning(
                "ignoring unreadable browser sessions at %s (%s: %s); browsers must sign in again",
                path,
                type(exc).__name__,
                exc,
            )


def _save_sessions() -> None:
    """Write the live sessions atomically with mode 0600; callers hold ``_sessions_lock``.
    Best-effort: on failure sessions keep working in memory and the cause is logged."""
    if _sessions_path is None:
        return
    now = _now()
    live = {d: e.isoformat() for d, e in _cookie_sessions.items() if e > now}
    saved = {"token": _token_fingerprint, "sessions": live}
    tmp = _sessions_path.with_name(f".{_sessions_path.name}.tmp")
    try:
        _sessions_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _sessions_path)
    except OSError:
        log.exception("could not save browser sessions to %s", _sessions_path)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            log.exception("could not remove the partial sessions file %s", tmp)


def start_cookie_session() -> str:
    """A new browser session; the returned value goes in the cookie and nowhere else."""
    session_id = secrets.token_urlsafe(32)
    with _sessions_lock:
        _cookie_sessions[_hash(session_id)] = _now() + SESSION_TTL
        _save_sessions()
    return session_id


def _cookie_session_is_valid(request: Request, session_id: str, *, can_renew: bool) -> bool:
    """Whether ``session_id`` is live. With ``can_renew`` it also renews a day-old session
    and flags the cookie for re-sending, so a browser in use never reaches the 30-day
    expiry; only requests whose response headers are still unsent may renew."""
    digest = _hash(session_id)
    with _sessions_lock:
        expires = _cookie_sessions.get(digest)
        if expires is None:
            return False
        now = _now()
        if expires <= now:
            del _cookie_sessions[digest]
            _save_sessions()
            return False
        is_renewal_due = can_renew and expires - now < SESSION_TTL - SESSION_RENEW_AFTER
        if is_renewal_due:
            _cookie_sessions[digest] = now + SESSION_TTL
            _save_sessions()
    if is_renewal_due:
        setattr(request.state, _RENEW_STATE_KEY, session_id)
    return True


def _cookie_header(session_id: str) -> str:
    max_age = int(SESSION_TTL.total_seconds())
    return f"{COOKIE}={session_id}; HttpOnly; Max-Age={max_age}; Path=/; SameSite=strict"


class SessionCookieRenewal:
    """Re-sends the session cookie with a fresh ``Max-Age`` when ``require_user`` renewed
    the session during this request (a dependency cannot reach the final response)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                renewed = scope.get("state", {}).get(_RENEW_STATE_KEY)
                if renewed is not None:
                    headers = list(message.get("headers", []))
                    headers.append((b"set-cookie", _cookie_header(renewed).encode("latin-1")))
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def issue_login_nonce(ttl: timedelta = DEFAULT_NONCE_TTL) -> str:
    nonce = secrets.token_urlsafe(24)
    _nonces[nonce] = datetime.now(UTC) + ttl
    return nonce


def register_session_token(token: str, session_id: str) -> None:
    _session_tokens[token] = session_id


def session_for_token(token: str) -> str | None:
    return _session_tokens.get(token)


def revoke_session_token(token: str) -> None:
    _session_tokens.pop(token, None)


def _consume_nonce(nonce: str) -> bool:
    expires = _nonces.pop(nonce, None)
    return expires is not None and expires > datetime.now(UTC)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def require_user(request: Request) -> Principal:
    """FastAPI dependency: the session cookie, the install token, or a session token."""
    return _authenticate(request, can_renew=True)


def _authenticate(request: Request, *, can_renew: bool) -> Principal:
    cfg: Config = request.app.state.ctx.cfg
    cookie = request.cookies.get(COOKIE)
    if cookie and _cookie_session_is_valid(request, cookie, can_renew=can_renew):
        return Principal("user")
    token = _bearer(request)
    if token is not None:
        if secrets.compare_digest(token.encode(), cfg.token.encode()):
            return Principal("user")
        session_id = _session_tokens.get(token)
        if session_id is not None:
            return Principal("session", session_id)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


def is_authorized(request: Request) -> bool:
    """Whether the request's credentials are still valid (long-lived streams recheck). Never
    renews: a stream's headers are already sent, so a renewed cookie could not reach the
    browser. The stream's reconnect goes through ``require_user`` and renews then."""
    try:
        _authenticate(request, can_renew=False)
    except HTTPException:
        return False
    return True


def require_owner(principal: Annotated[Principal, Depends(require_user)]) -> Principal:
    """Like ``require_user`` but refuses agent session tokens."""
    if principal.kind != "user":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not available to agent sessions")
    return principal


def auth_router() -> APIRouter:
    router = APIRouter()

    @router.get("/login", include_in_schema=False)
    async def login(nonce: Annotated[str, Query(min_length=1)]) -> RedirectResponse:
        if not _consume_nonce(nonce):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login link expired or used")
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.headers.append("set-cookie", _cookie_header(start_cookie_session()))
        return response

    @router.post("/api/auth/nonce")
    async def new_nonce(
        request: Request, _: Annotated[Principal, Depends(require_owner)]
    ) -> dict[str, str]:
        cfg: Config = request.app.state.ctx.cfg
        nonce = issue_login_nonce()
        return {"nonce": nonce, "url": f"http://127.0.0.1:{cfg.port}/login?nonce={nonce}"}

    @router.post("/api/auth/rotate")
    async def rotate(
        request: Request, _: Annotated[Principal, Depends(require_owner)]
    ) -> dict[str, object]:
        """Settings → Rotate token: a new install token; every browser is signed out
        (a new login link comes from ``jesinia status``) and agents are re-registered."""
        ctx = request.app.state.ctx
        path = ctx.cfg.data_dir / "config.toml"
        fresh = replace(load_config(path), token=secrets.token_urlsafe(32))
        save_config(fresh, path)
        ctx.cfg = replace(ctx.cfg, token=fresh.token)
        invalidate_sessions(fresh.token)
        hook = ctx.services.get("auth.token_rotated")
        if callable(hook):
            # Re-registering agents is secondary: the rotation already succeeded.
            try:
                await asyncio.to_thread(hook, ctx.cfg)
            except Exception as exc:
                log.exception("token rotated, but refreshing agent registrations failed")
                return {
                    "ok": True,
                    "agents_refreshed": False,
                    "warning": f"Agents were not re-registered ({type(exc).__name__}: {exc}). "
                    "Run `jesinia token rotate` or ./setup again.",
                }
        return {"ok": True, "agents_refreshed": True}

    @router.post("/api/auth/reload-token")
    async def reload_token(
        request: Request, _: Annotated[Principal, Depends(require_owner)]
    ) -> dict[str, bool]:
        """Adopt the token now in ``config.toml`` (after ``jesinia token rotate``) and sign
        out every browser session. Authenticated with the token being replaced."""
        ctx = request.app.state.ctx
        fresh = load_config(ctx.cfg.data_dir / "config.toml")
        ctx.cfg = replace(ctx.cfg, token=fresh.token)
        invalidate_sessions(fresh.token)
        return {"ok": True}

    @router.get("/api/ping")
    async def ping(principal: Annotated[Principal, Depends(require_user)]) -> dict[str, object]:
        body: dict[str, object] = {"ok": True, "principal": principal.kind}
        if principal.session_id is not None:
            body["session_id"] = principal.session_id
        return body

    return router
