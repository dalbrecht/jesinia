# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jesinia.core.app import create_app
from jesinia.core.config import Config, load_config
from jesinia.core.idempotency import IDEMPOTENCY_HEADER
from jesinia.core.module import CallContext, ToolDef
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.module import KbModule
from tests.conftest import BASE_URL, bearer, lifespan_client

TAGS = "/api/kb/tags"


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


@pytest.fixture
def kb_cfg(cfg: Config, kb_root: Path) -> Config:
    load_config(cfg.data_dir / "config.toml")
    return replace(cfg, kb_path=kb_root)


@pytest.fixture
def module() -> KbModule:
    return KbModule()


@pytest.fixture
def app(kb_cfg: Config, module: KbModule) -> FastAPI:
    return create_app(kb_cfg, [module])


async def _fresh_key(request: httpx.Request) -> None:
    """Like the web client: each request is a new user action unless a test reuses a key."""
    request.headers.setdefault(IDEMPOTENCY_HEADER, str(uuid.uuid4()))


@pytest.fixture
async def client(app: FastAPI, kb_cfg: Config) -> AsyncIterator[httpx.AsyncClient]:
    async with lifespan_client(app) as c:
        c.headers.update(bearer(kb_cfg.token))
        c.event_hooks["request"].append(_fresh_key)
        yield c


async def _page(c: httpx.AsyncClient, title: str, body: str) -> dict[str, str]:
    r = await c.put("/api/kb/pages", json={"title": title, "folder": "Notes", "body": body})
    assert r.status_code == 200, r.text
    page: dict[str, str] = r.json()
    return page


async def _add(c: httpx.AsyncClient, tag: str, **extra: object) -> httpx.Response:
    return await c.post(TAGS, json={"tag": tag, **extra})


async def test_add_and_list_catalog(client: httpx.AsyncClient) -> None:
    assert (await client.get(TAGS)).json() == []
    assert (await _add(client, "SQLite", meaning="db")).status_code == 204
    await _page(client, "A", "#sqlite")
    assert (await client.get(TAGS)).json() == [
        {"tag": "sqlite", "meaning": "db", "aliases": [], "retired": False, "count": 1}
    ]


async def test_add_conflicts_return_409(client: httpx.AsyncClient) -> None:
    await _add(client, "database")
    exists = await _add(client, "Database")
    assert exists.status_code == 409 and exists.json()["error"] == "TagExists"
    near = await _add(client, "databases")
    assert near.status_code == 409
    assert near.json()["error"] == "NearDuplicate" and near.json()["near"] == ["database"]
    assert (await _add(client, "databases", force=True)).status_code == 204
    assert (await _add(client, "a/b")).status_code == 400
    assert (await _add(client, "")).status_code == 422


async def test_check_route(client: httpx.AsyncClient) -> None:
    await _add(client, "database")
    r = await client.get(f"{TAGS}/check", params={"tag": "Databases"})
    assert r.status_code == 200
    assert r.json() == {
        "normalised": "databases",
        "exists": False,
        "canonical": None,
        "near": ["database"],
        "same_meaning": None,
    }
    assert (await client.get(f"{TAGS}/check", params={"tag": "#"})).status_code == 400


async def test_suggest_route(client: httpx.AsyncClient) -> None:
    await _add(client, "sqlite")
    r = await client.post(f"{TAGS}/suggest", json={"text": "SQLite notes", "limit": 3})
    assert r.status_code == 200 and r.json() == [{"tag": "sqlite", "new": False}]
    assert (await client.post(f"{TAGS}/suggest", json={"text": "x", "limit": 0})).status_code == 422


async def test_uncataloged_route(client: httpx.AsyncClient) -> None:
    await _page(client, "A", "#rust #rust")
    r = await client.get(f"{TAGS}/uncataloged")
    assert r.status_code == 200 and r.json() == [{"tag": "rust", "count": 1}]


async def test_rename_preview_and_apply_routes(client: httpx.AsyncClient) -> None:
    page = await _page(client, "A", "#old")
    preview = await client.post(f"{TAGS}/rename/preview", json={"old": "old", "new": "new"})
    assert preview.status_code == 200
    assert preview.json() == {
        "pages": [{"id": page["id"], "title": "A", "rev": page["rev"]}],
        "skipped_reserved": [],
    }
    revs = {p["id"]: p["rev"] for p in preview.json()["pages"]}
    r = await client.post(f"{TAGS}/rename", json={"old": "old", "new": "new", "revs": revs})
    assert r.status_code == 200 and r.json() == {"updated": [page["id"]], "skipped_changed": []}
    bad = await client.post(f"{TAGS}/rename", json={"old": "x", "new": "X", "revs": {}})
    assert bad.status_code == 400


async def test_merge_route(client: httpx.AsyncClient) -> None:
    page = await _page(client, "A", "#ml #ai")
    r = await client.post(
        f"{TAGS}/merge",
        json={"sources": ["ml", "ai"], "into": "machine-learning", "revs": {page["id"]: "stale"}},
    )
    assert r.status_code == 200 and r.json() == {"updated": [], "skipped_changed": [page["id"]]}
    assert (await client.get(TAGS)).json()[0]["aliases"] == ["ml", "ai"]
    empty = await client.post(f"{TAGS}/merge", json={"sources": [], "into": "x", "revs": {}})
    assert empty.status_code == 422


async def test_retire_adopt_ignore_routes(client: httpx.AsyncClient) -> None:
    assert (await client.post(f"{TAGS}/retire", json={"tag": "ghost"})).status_code == 404
    await _page(client, "A", "#wal #noise")
    assert (await client.post(f"{TAGS}/adopt", json={"tag": "wal"})).status_code == 204
    assert (await client.post(f"{TAGS}/adopt", json={"tag": "wal"})).status_code == 409
    assert (await client.post(f"{TAGS}/ignore", json={"tag": "noise"})).status_code == 204
    assert (await client.post(f"{TAGS}/retire", json={"tag": "wal"})).status_code == 204
    assert (await client.get(TAGS)).json()[0]["retired"] is True
    assert (await client.get(f"{TAGS}/uncataloged")).json() == []


async def test_restore_unretires_a_tag(client: httpx.AsyncClient) -> None:
    await _add(client, "wal")
    assert (await client.post(f"{TAGS}/retire", json={"tag": "wal"})).status_code == 204
    assert (await client.post(f"{TAGS}/restore", json={"tag": "WAL"})).status_code == 204
    assert (await client.get(TAGS)).json()[0]["retired"] is False
    missing = await client.post(f"{TAGS}/restore", json={"tag": "ghost"})
    assert missing.status_code == 404 and missing.json()["error"] == "TagNotFound"


async def test_rename_retry_does_not_rewrite_twice(client: httpx.AsyncClient) -> None:
    page = await _page(client, "A", "#old")
    preview = await client.post(f"{TAGS}/rename/preview", json={"old": "old", "new": "new"})
    revs = {p["id"]: p["rev"] for p in preview.json()["pages"]}
    body = {"old": "old", "new": "new", "revs": revs}
    key = {IDEMPOTENCY_HEADER: "rename-1"}
    first = await client.post(f"{TAGS}/rename", json=body, headers=key)
    after_first = (await client.get(f"/api/kb/pages/{page['id']}")).json()
    retry = await client.post(f"{TAGS}/rename", json=body, headers=key)
    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json() == {"updated": [page["id"]], "skipped_changed": []}
    assert (await client.get(f"/api/kb/pages/{page['id']}")).json() == after_first


async def test_tag_writes_require_a_key_and_reads_do_not(
    app: FastAPI, client: httpx.AsyncClient, kb_cfg: Config
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=BASE_URL, headers=bearer(kb_cfg.token)
    ) as bare:
        for path, body in [
            ("", {"tag": "x"}),
            ("/rename", {"old": "a", "new": "b", "revs": {}}),
            ("/merge", {"sources": ["a"], "into": "b", "revs": {}}),
            ("/retire", {"tag": "x"}),
            ("/restore", {"tag": "x"}),
            ("/adopt", {"tag": "x"}),
            ("/ignore", {"tag": "x"}),
        ]:
            r = await bare.post(f"{TAGS}{path}", json=body)
            assert r.status_code == 400, path
            assert r.json()["error"] == "IdempotencyKeyMissing", path
        assert (await bare.get(TAGS)).status_code == 200
        assert (await bare.get(f"{TAGS}/check", params={"tag": "x"})).status_code == 200
        assert (await bare.post(f"{TAGS}/suggest", json={"text": "x"})).status_code == 200
        preview = await bare.post(f"{TAGS}/rename/preview", json={"old": "a", "new": "b"})
        assert preview.status_code == 200
    assert (await client.get(TAGS)).json() == []


async def test_stale_catalog_write_returns_409(
    client: httpx.AsyncClient, module: KbModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jesinia.services.kb.application.errors import StaleWrite

    async def stale(*args: object, **kwargs: object) -> None:
        raise StaleWrite("catalog changed")

    monkeypatch.setattr(module.tags, "add", stale)
    r = await _add(client, "x")
    assert r.status_code == 409 and r.json()["error"] == "StaleWrite"


async def test_service_is_registered(client: httpx.AsyncClient, module: KbModule) -> None:
    assert isinstance(module.tags, TagService)


def test_tags_unavailable_before_start() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _ = KbModule().tags
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        _ = KbModule().idempotency
    assert exc.value.status_code == 503


async def test_daily_job_prunes_keys_older_than_seven_days(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    from datetime import UTC, datetime, timedelta

    from jesinia.services.kb.module import IDEMPOTENCY_PRUNE_JOB

    now = datetime.now(UTC)
    module.idempotency.reserve("old", "POST /x", now - timedelta(days=8))
    module.idempotency.reserve("recent", "POST /x", now - timedelta(days=6))
    assert module._ctx is not None
    await module._ctx.scheduler.run_now(IDEMPOTENCY_PRUNE_JOB)
    assert module.idempotency.prune(now) == 1  # only "recent" was left
    await module.stop()
    await module.prune_idempotency_keys()  # stopped: nothing to prune, no error


def _tools(module: KbModule) -> dict[str, ToolDef]:
    return {t.name: t for t in module.mcp_tools() if t.name.startswith("tags_")}


async def test_tools_are_read_only(client: httpx.AsyncClient, module: KbModule) -> None:
    tools = _tools(module)
    assert set(tools) == {"tags_list", "tags_suggest", "tags_check"}
    assert not any(t.writes for t in tools.values())


async def test_tools_match_the_routes(client: httpx.AsyncClient, module: KbModule) -> None:
    await _add(client, "sqlite")
    await _page(client, "A", "#sqlite")
    tools = _tools(module)

    async def call(name: str, **args: object) -> object:
        tool = tools[name]
        return await tool.handler(tool.input_model(**args), CallContext("s-1"))

    assert await call("tags_list") == (await client.get(TAGS)).json()
    assert await call("tags_suggest", text="sqlite") == [{"tag": "sqlite", "new": False}]
    checked = await call("tags_check", tag="SQLite")
    assert isinstance(checked, dict) and checked["canonical"] == "sqlite"
