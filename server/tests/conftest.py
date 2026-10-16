# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jesinia.core import auth
from jesinia.core.config import Config

BASE_URL = "http://127.0.0.1:5374"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(token="t" * 43, data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


@pytest.fixture(autouse=True)
def _reset_auth() -> Iterator[None]:
    auth.reset_state()
    yield
    auth.reset_state()


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    """Run the app's lifespan in one dedicated task, as uvicorn does.

    Entering and exiting the lifespan from different tasks (pytest-asyncio fixtures do)
    breaks anyio cancel scopes such as the MCP session manager's.
    """
    started, stop = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run())
    waiter = asyncio.create_task(started.wait())
    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        waiter.cancel()
        await task  # re-raises a startup failure
    try:
        yield
    finally:
        stop.set()
        await task


@asynccontextmanager
async def lifespan_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client against ``app`` with its lifespan (startup/shutdown) running."""
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
