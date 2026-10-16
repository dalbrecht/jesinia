# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from jesinia.core.idempotency import (
    IDEMPOTENCY_HEADER,
    IdempotencyInProgress,
    IdempotentCall,
    SqliteIdempotencyStore,
    StoredResponse,
    idempotent,
)

BASE_URL = "http://127.0.0.1:5374"
TABLE = (
    "CREATE TABLE idempotency (key TEXT NOT NULL, route TEXT NOT NULL, status INTEGER, "
    "body TEXT, created_at TEXT NOT NULL, PRIMARY KEY (key, route))"
)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute(TABLE)
    yield c
    c.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SqliteIdempotencyStore:
    return SqliteIdempotencyStore(conn)


class Counter:
    """What the test routes did: how many times each ran, and a gate to hold one open."""

    def __init__(self) -> None:
        self.runs: dict[str, int] = {}
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.fail_with: int | None = None

    def hit(self, name: str) -> int:
        self.runs[name] = self.runs.get(name, 0) + 1
        return self.runs[name]


@pytest.fixture
def counter() -> Counter:
    return Counter()


@pytest.fixture
def app(store: SqliteIdempotencyStore, counter: Counter) -> FastAPI:
    app = FastAPI()
    keyed = Depends(idempotent(lambda: store, now=lambda: NOW))

    @app.post("/a")
    async def a(idem: IdempotentCall = keyed) -> Response:
        async def run() -> Response:
            n = counter.hit("a")
            if counter.fail_with is not None:
                return JSONResponse({"error": "Boom"}, status_code=counter.fail_with)
            return JSONResponse({"run": n}, status_code=201)

        return await idem.run(run)

    @app.post("/b")
    async def b(idem: IdempotentCall = keyed) -> Response:
        async def run() -> Response:
            counter.hit("b")
            return Response(status_code=204)

        return await idem.run(run)

    @app.post("/slow")
    async def slow(idem: IdempotentCall = keyed) -> Response:
        async def run() -> Response:
            counter.hit("slow")
            counter.entered.set()
            await counter.gate.wait()
            return JSONResponse({"done": True})

        return await idem.run(run)

    @app.post("/raises")
    async def raises(idem: IdempotentCall = keyed) -> Response:
        async def run() -> Response:
            counter.hit("raises")
            raise RuntimeError("handler blew up")

        return await idem.run(run)

    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        yield c


def _key(value: str) -> dict[str, str]:
    return {IDEMPOTENCY_HEADER: value}


async def test_repeated_key_returns_stored_response_without_acting(
    client: httpx.AsyncClient, counter: Counter
) -> None:
    first = await client.post("/a", headers=_key("k1"))
    again = await client.post("/a", headers=_key("k1"))
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json() == {"run": 1}
    assert counter.runs == {"a": 1}
    other = await client.post("/a", headers=_key("k2"))
    assert other.json() == {"run": 2}


async def test_missing_key_is_400(client: httpx.AsyncClient, counter: Counter) -> None:
    r = await client.post("/a")
    assert r.status_code == 400
    assert r.json()["error"] == "IdempotencyKeyMissing" and r.json()["detail"]
    assert counter.runs == {}


async def test_key_longer_than_200_or_empty_is_400(
    client: httpx.AsyncClient, counter: Counter
) -> None:
    long = await client.post("/a", headers=_key("x" * 201))
    assert long.status_code == 400 and long.json()["error"] == "IdempotencyKeyInvalid"
    empty = await client.post("/a", headers=_key(""))
    assert empty.status_code == 400
    assert (await client.post("/a", headers=_key("x" * 200))).status_code == 201
    assert counter.runs == {"a": 1}


async def test_same_key_different_route_is_independent(
    client: httpx.AsyncClient, counter: Counter
) -> None:
    a = await client.post("/a", headers=_key("same"))
    b = await client.post("/b", headers=_key("same"))
    assert a.status_code == 201 and b.status_code == 204
    replay = await client.post("/b", headers=_key("same"))
    assert replay.status_code == 204 and replay.content == b""
    assert counter.runs == {"a": 1, "b": 1}


async def test_concurrent_request_with_the_same_key_is_409(
    client: httpx.AsyncClient, counter: Counter
) -> None:
    first = asyncio.create_task(client.post("/slow", headers=_key("k")))
    await counter.entered.wait()
    second = await client.post("/slow", headers=_key("k"))
    assert second.status_code == 409
    assert second.json() == {
        "error": "IdempotencyInProgress",
        "detail": "a request with this key is still in progress",
    }
    counter.gate.set()
    assert (await first).json() == {"done": True}
    assert (await client.post("/slow", headers=_key("k"))).json() == {"done": True}
    assert counter.runs == {"slow": 1}


async def test_server_errors_are_not_stored_so_a_retry_runs_again(
    client: httpx.AsyncClient, counter: Counter
) -> None:
    counter.fail_with = 503
    assert (await client.post("/a", headers=_key("k"))).status_code == 503
    counter.fail_with = None
    assert (await client.post("/a", headers=_key("k"))).json() == {"run": 2}
    counter.fail_with = 409
    assert (await client.post("/a", headers=_key("c"))).status_code == 409
    counter.fail_with = None
    assert (await client.post("/a", headers=_key("c"))).status_code == 409  # 4xx is replayed
    assert counter.runs == {"a": 3}


async def test_a_handler_exception_releases_the_key(
    client: httpx.AsyncClient, counter: Counter
) -> None:
    assert (await client.post("/raises", headers=_key("k"))).status_code == 500
    assert (await client.post("/raises", headers=_key("k"))).status_code == 500
    assert counter.runs == {"raises": 2}


def test_store_reserves_completes_and_releases(store: SqliteIdempotencyStore) -> None:
    assert store.reserve("k", "POST /a", NOW) is None
    with pytest.raises(IdempotencyInProgress):
        store.reserve("k", "POST /a", NOW)
    store.complete("k", "POST /a", StoredResponse(200, '{"ok":true}'))
    assert store.reserve("k", "POST /a", NOW) == StoredResponse(200, '{"ok":true}')
    store.release("k", "POST /a")
    assert store.reserve("k", "POST /a", NOW) is None


def test_prune_removes_keys_older_than_the_cutoff(
    store: SqliteIdempotencyStore, conn: sqlite3.Connection
) -> None:
    store.reserve("old", "r", NOW - timedelta(days=8))
    store.complete("old", "r", StoredResponse(204, None))
    store.reserve("new", "r", NOW - timedelta(days=6))
    assert store.prune(NOW - timedelta(days=7)) == 1
    assert [row[0] for row in conn.execute("SELECT key FROM idempotency")] == ["new"]


def test_clear_pending_drops_unfinished_reservations_only(
    store: SqliteIdempotencyStore,
) -> None:
    store.reserve("crashed", "POST /a", NOW)
    store.reserve("done", "POST /a", NOW)
    store.complete("done", "POST /a", StoredResponse(204, None))
    assert store.clear_pending() == 1
    assert store.reserve("crashed", "POST /a", NOW) is None  # free to act again
    assert store.reserve("done", "POST /a", NOW) == StoredResponse(204, None)
