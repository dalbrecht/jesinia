# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication (spec §7.4).

- PWAs: a short-lived, single-use login nonce (printed by ``./setup`` / ``jesinia status``)
  is exchanged at ``/login`` for an ``HttpOnly; SameSite=Strict`` session cookie. The
  install token never appears in a URL.
- MCP clients and the CLI: ``Authorization: Bearer <install token>``.
- Agent sessions launched by Jesinia: per-session bearer tokens (plan Task 18).

State is process-local: the server is a single process (spec §6).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from jesinia.core.config import Config, load_config, save_config

COOKIE = "jesinia_session"
DEFAULT_NONCE_TTL = timedelta(minutes=5)

log = logging.getLogger(__name__)
_nonces: dict[str, datetime] = {}
_cookie_sessions: set[str] = set()
_session_tokens: dict[str, str] = {}


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "session"]
    session_id: str | None = None


def reset_state() -> None:
    """Forget all nonces, cookie sessions, and session tokens (tests, token rotation)."""
    _nonces.clear()
    _cookie_sessions.clear()
    _session_tokens.clear()


def invalidate_sessions() -> None:
    """Log out every browser (``jesinia token rotate``)."""
    _cookie_sessions.clear()


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
    cfg: Config = request.app.state.ctx.cfg
    cookie = request.cookies.get(COOKIE)
    if cookie and cookie in _cookie_sessions:
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
    """Whether the request's credentials are still valid (long-lived streams recheck)."""
    try:
        require_user(request)
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
        sid = secrets.token_urlsafe(32)
        _cookie_sessions.add(sid)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", path="/")
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
        invalidate_sessions()
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
        invalidate_sessions()
        return {"ok": True}

    @router.get("/api/ping")
    async def ping(principal: Annotated[Principal, Depends(require_user)]) -> dict[str, object]:
        body: dict[str, object] = {"ok": True, "principal": principal.kind}
        if principal.session_id is not None:
            body["session_id"] = principal.session_id
        return body

    return router
