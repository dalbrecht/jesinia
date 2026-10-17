# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.module import CallContext
from jesinia.services.kb.module import KbModule
from jesinia.services.search.application.service import SearchService
from jesinia.services.search.module import SearchModule
from tests.conftest import bearer, lifespan_client


@pytest.fixture
def kb_cfg(cfg: Config, tmp_path: Path) -> Config:
    root = tmp_path / "kb"
    (root / "Notes").mkdir(parents=True)
    for i in range(3):
        (root / "Notes" / f"Page {i}.md").write_text(f"shared word and unique{i}\n")
    return replace(cfg, kb_path=root)


@pytest.fixture
async def client(kb_cfg: Config) -> AsyncIterator[tuple[httpx.AsyncClient, SearchModule]]:
    search = SearchModule()
    async with lifespan_client(create_app(kb_cfg, [KbModule(), search])) as c:
        c.headers.update(bearer(kb_cfg.token))
        for _ in range(50):
            if not search.service.indexing:
                break
            await asyncio.sleep(0.02)
        yield c, search


async def test_route_and_tools(client: tuple[httpx.AsyncClient, SearchModule]) -> None:
    c, search = client
    hits = (await c.get("/api/search", params={"q": "unique1"})).json()
    assert [h["title"] for h in hits] == ["Page 1"] and hits[0]["source"] == "kb"
    assert len((await c.get("/api/search", params={"q": "shared", "limit": 2})).json()) == 2
    assert (await c.get("/api/search", params={"q": "shared", "source": "rss"})).json() == []
    tools = {t.name: t for t in search.mcp_tools()}
    assert set(tools) == {"search_query", "kb_search"} and not any(t.writes for t in tools.values())
    via_tool = await tools["kb_search"].handler(
        tools["kb_search"].input_model(q="unique2"), CallContext(None)
    )
    assert isinstance(via_tool, list) and via_tool[0]["title"] == "Page 2"
    q = tools["search_query"]
    assert await q.handler(q.input_model(q="shared", sources=["rss"]), CallContext(None)) == []
    assert (await c.get("/healthz")).json()["modules"]["search"] == {"ok": True, "detail": "ready"}


async def test_startup_reindex_does_not_block_healthz(
    kb_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SearchService._index_page
    release = asyncio.Event()

    async def held(self: SearchService, id):  # type: ignore[no-untyped-def]
        # The reindex cannot finish until the test releases it, so healthz answering at all
        # (and saying "indexing") proves it does not wait for the reindex; no clock involved.
        await release.wait()
        await original(self, id)

    monkeypatch.setattr(SearchService, "_index_page", held)
    async with lifespan_client(create_app(kb_cfg, [KbModule(), SearchModule()])) as c:
        body = (await asyncio.wait_for(c.get("/healthz"), timeout=10)).json()
        assert body["modules"]["search"] == {"ok": True, "detail": "indexing"}
    # Never released: leaving the app cancels the held reindex, which must shut down cleanly.
    assert not release.is_set()


async def test_requires_kb_first(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [SearchModule()])) as c:
        detail = (await c.get("/healthz")).json()["modules"]["search"]["detail"]
        assert "requires the kb service" in detail


async def test_register_rejects_kb_source(client: tuple[httpx.AsyncClient, SearchModule]) -> None:
    from jesinia.services.search.application.service import ExternalRecord

    _, search = client
    with pytest.raises(ValueError):
        await search.service.register(ExternalRecord("kb", "x", "t", "b", None))


def test_service_before_start() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _ = SearchModule().service


async def test_failed_startup_reindex_is_reported(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:

    async def broken(self: SearchService, id: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(SearchService, "_index_page", broken)
    root = cfg.data_dir.parent / "kb-r"
    root.mkdir()
    (root / "page.md").write_text("one page, so the rebuild has something to index\n")
    mod = SearchModule()
    async with lifespan_client(create_app(replace(cfg, kb_path=root), [KbModule(), mod])) as c:
        for _ in range(50):
            h = (await c.get("/healthz")).json()["modules"]["search"]
            if not h["ok"]:
                break
            await asyncio.sleep(0.01)
        assert h == {
            "ok": False,
            "detail": "reindex failed: database is locked (run `jesinia reindex`)",
        }
