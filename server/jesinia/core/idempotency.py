# SPDX-License-Identifier: AGPL-3.0-or-later
"""Idempotency keys for non-idempotent routes (``Idempotency-Key`` header).

A write route takes an ``IdempotentCall`` dependency (built by ``idempotent``) and runs its
work through ``IdempotentCall.run``. The first request with a ``(key, route)`` pair reserves
it, runs, and stores the response status and JSON body; a repeat gets that stored response
back without acting again. A request that arrives while the first is still running gets 409.
Server errors (5xx), transient answers (any response with a ``Retry-After`` header: "not
yet", never a final outcome) and handler exceptions release the reservation so the client
can retry with the same key.

Core owns no database: each module that uses this adds the ``idempotency`` table to its own
migrations and hands ``idempotent`` a store over its own connection::

    CREATE TABLE idempotency (
        key TEXT NOT NULL,
        route TEXT NOT NULL,
        status INTEGER,          -- NULL while the first request is still running
        body TEXT,               -- the JSON body; NULL for an empty one
        created_at TEXT NOT NULL,
        PRIMARY KEY (key, route)
    );

The keys live in the owning module's database, so rebuilding a DERIVED database
(``jesinia reindex``) forgets them. At start the module calls ``clear_pending``: nothing is in
flight then, so a pending row was left by a crash and would otherwise block retries with 409.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

from fastapi import Header, Request, status
from fastapi.responses import JSONResponse, Response

IDEMPOTENCY_HEADER = "Idempotency-Key"
KEY_MAX = 200
# How long a key is remembered; owning modules prune older keys daily.
RETENTION = timedelta(days=7)


class IdempotencyError(Exception):
    """Base for idempotency failures."""


class IdempotencyInProgress(IdempotencyError):  # noqa: N818 - the error code clients see
    """Another request with the same key and route has not finished yet."""


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: str | None  # JSON text; None for an empty body (e.g. 204)


class IdempotencyStore(Protocol):
    """Where reservations and finished responses live (one per owning module's database)."""

    def reserve(self, key: str, route: str, now: datetime) -> StoredResponse | None:
        """Reserve ``(key, route)`` and return None, or return its stored response.

        Raises ``IdempotencyInProgress`` when it is reserved but not yet complete.
        """
        ...

    def complete(self, key: str, route: str, response: StoredResponse) -> None:
        """Store the response for a reserved ``(key, route)``."""
        ...

    def release(self, key: str, route: str) -> None:
        """Drop a reservation so the same key can be retried."""
        ...

    def prune(self, before: datetime) -> int:
        """Forget keys created before ``before``; how many were removed."""
        ...

    def clear_pending(self) -> int:
        """Drop reservations that never completed (call at start, before serving)."""
        ...


class SqliteIdempotencyStore:
    """``IdempotencyStore`` over a module's own (autocommit) connection from ``open_db``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def reserve(self, key: str, route: str, now: datetime) -> StoredResponse | None:
        # A single INSERT is atomic, so of two racing requests exactly one wins the row.
        inserted = self._conn.execute(
            "INSERT INTO idempotency (key, route, status, body, created_at) "
            "VALUES (?, ?, NULL, NULL, ?) ON CONFLICT (key, route) DO NOTHING",
            (key, route, now.isoformat()),
        )
        if inserted.rowcount == 1:
            return None
        found = self._conn.execute(
            "SELECT status, body FROM idempotency WHERE key = ? AND route = ?", (key, route)
        ).fetchone()
        # The conflicting row exists: nothing else writes this table between the two statements.
        if found[0] is None:
            raise IdempotencyInProgress("a request with this key is still in progress")
        return StoredResponse(int(found[0]), found[1])

    def complete(self, key: str, route: str, response: StoredResponse) -> None:
        self._conn.execute(
            "UPDATE idempotency SET status = ?, body = ? WHERE key = ? AND route = ?",
            (response.status, response.body, key, route),
        )

    def release(self, key: str, route: str) -> None:
        self._conn.execute("DELETE FROM idempotency WHERE key = ? AND route = ?", (key, route))

    def prune(self, before: datetime) -> int:
        removed = self._conn.execute(
            "DELETE FROM idempotency WHERE created_at < ?", (before.isoformat(),)
        )
        return removed.rowcount

    def clear_pending(self) -> int:
        cleared = self._conn.execute("DELETE FROM idempotency WHERE status IS NULL")
        return cleared.rowcount


def _refusal(code: int, error: str, detail: str) -> JSONResponse:
    # The same ``{"error", "detail"}`` shape the service routes use for their typed errors.
    return JSONResponse({"error": error, "detail": detail}, status_code=code)


def _replay(stored: StoredResponse) -> Response:
    if stored.body is None:
        return Response(status_code=stored.status)
    return Response(stored.body, status_code=stored.status, media_type="application/json")


class IdempotentCall:
    """One request's key and route; ``run`` executes the route's work at most once."""

    def __init__(self, store: IdempotencyStore, key: str | None, route: str, now: datetime) -> None:
        self._store = store
        self._key = key
        self._route = route
        self._now = now

    @property
    def key(self) -> str | None:
        """The request's key (``None`` when missing; ``run`` refuses such a request)."""
        return self._key

    async def run(self, work: Callable[[], Awaitable[Response]]) -> Response:
        """The stored response for a repeated key, else ``work()``'s (stored unless it is a
        5xx or carries ``Retry-After``).

        A missing or malformed key is 400; a key whose first request is still running is
        409. If ``work`` raises, the reservation is released and the exception propagates.
        """
        key = self._key
        if key is None:
            return _refusal(
                status.HTTP_400_BAD_REQUEST,
                "IdempotencyKeyMissing",
                f"the {IDEMPOTENCY_HEADER} header is required for this request",
            )
        if not 1 <= len(key) <= KEY_MAX:
            return _refusal(
                status.HTTP_400_BAD_REQUEST,
                "IdempotencyKeyInvalid",
                f"the {IDEMPOTENCY_HEADER} header must be 1 to {KEY_MAX} characters",
            )
        try:
            stored = self._store.reserve(key, self._route, self._now)
        except IdempotencyInProgress as exc:
            return _refusal(status.HTTP_409_CONFLICT, type(exc).__name__, str(exc))
        if stored is not None:
            return _replay(stored)
        try:
            response = await work()
        except BaseException:
            self._store.release(key, self._route)
            raise
        transient = "retry-after" in response.headers
        if transient or response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            self._store.release(key, self._route)
        else:
            body = bytes(response.body).decode("utf-8") or None
            self._store.complete(key, self._route, StoredResponse(response.status_code, body))
        return response


def idempotent(
    store: Callable[[], IdempotencyStore],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[..., Awaitable[IdempotentCall]]:
    """A FastAPI dependency giving a route its ``IdempotentCall``.

    ``store`` is called per request, so a module can wire it before its database opens
    (routers are built before ``start``). Use it as
    ``Annotated[IdempotentCall, Depends(idempotent(lambda: module.idempotency))]``.
    The header is optional in the schema so a missing key is the documented 400, not a 422.
    """

    async def dependency(
        request: Request,
        key: Annotated[
            str | None,
            Header(
                alias=IDEMPOTENCY_HEADER,
                description=f"A client-chosen key (1-{KEY_MAX} characters), one per user "
                "action; a retry of the same action reuses it and gets the first response.",
            ),
        ] = None,
    ) -> IdempotentCall:
        return IdempotentCall(store(), key, f"{request.method} {request.url.path}", now())

    return dependency
