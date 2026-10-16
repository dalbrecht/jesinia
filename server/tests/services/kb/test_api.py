# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from jesinia.core.app import create_app
from jesinia.core.config import Config, load_config
from jesinia.core.events import Event
from jesinia.core.module import CallContext
from jesinia.services.kb.module import KbModule
from tests.conftest import bearer, lifespan_client


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
async def client(kb_cfg: Config, module: KbModule) -> AsyncIterator[httpx.AsyncClient]:
    async with lifespan_client(create_app(kb_cfg, [module])) as c:
        c.headers.update(bearer(kb_cfg.token))
        yield c


async def _create(c: httpx.AsyncClient, title: str, body: str = "b", folder: str = "Notes") -> dict:  # type: ignore[type-arg]
    r = await c.put("/api/kb/pages", json={"title": title, "folder": folder, "body": body})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_create_read_update(client: httpx.AsyncClient) -> None:
    p = await _create(client, "Hello", "v1")
    got = (await client.get(f"/api/kb/pages/{p['id']}")).json()
    assert got["body"] == "v1" and got["rev"] == p["rev"] and got["path"] == "Notes/Hello.md"
    r = await client.patch(
        f"/api/kb/pages/{p['id']}/body", json={"body": "v2", "expected_rev": p["rev"]}
    )
    assert r.status_code == 200 and r.json()["body"] == "v2"


async def test_patch_body_returns_409_with_current_on_stale(
    client: httpx.AsyncClient, kb_root: Path
) -> None:
    p = await _create(client, "S", "v1")
    f = kb_root / "Notes/S.md"
    f.write_text(f.read_text().replace("v1", "user"))
    before = f.read_bytes()
    r = await client.patch(
        f"/api/kb/pages/{p['id']}/body", json={"body": "mine", "expected_rev": p["rev"]}
    )
    assert r.status_code == 409 and r.json()["body"] == "user" and r.json()["rev"] != p["rev"]
    assert f.read_bytes() == before


async def test_error_mapping(client: httpx.AsyncClient) -> None:
    missing = "0192f7a8-0000-7000-8000-0000000000ff"
    assert (await client.get(f"/api/kb/pages/{missing}")).status_code == 404
    bad = await client.put("/api/kb/pages", json={"title": "x", "folder": "../out"})
    assert bad.status_code == 400
    reserved = await client.put("/api/kb/pages", json={"title": "x", "folder": "Journal"})
    assert reserved.status_code == 400
    p = await _create(client, "R")
    stale = await client.put("/api/kb/pages", json={"id": p["id"], "title": "R", "folder": "Notes"})
    assert stale.status_code == 409
    gone = await client.patch(
        f"/api/kb/pages/{missing}/body", json={"body": "x", "expected_rev": "r"}
    )
    assert gone.status_code == 404


async def test_move_and_tree(client: httpx.AsyncClient) -> None:
    a = await _create(client, "Target", "t")
    await _create(client, "Linker", f"[[{a['id']}|Target]]")
    r = (
        await client.post(
            f"/api/kb/pages/{a['id']}/move", json={"folder": "Notes", "title": "Renamed"}
        )
    ).json()
    assert r["page"]["path"] == "Notes/Renamed.md" and len(r["relinked"]) == 1
    tree = (await client.get("/api/kb/tree")).json()
    assert tree[0]["path"] == "Notes" and tree[0]["pages"] == 2


async def test_tree_route_and_mcp_tool_match(client: httpx.AsyncClient, module: KbModule) -> None:
    await _create(client, "One")
    tools = {t.name: t for t in module.mcp_tools()}
    tool = tools["kb_tree"]
    via_mcp = await tool.handler(tool.input_model(), CallContext(None))
    assert (await client.get("/api/kb/tree")).json() == via_mcp


async def test_mcp_tools(client: httpx.AsyncClient, module: KbModule) -> None:
    tools = {t.name: t for t in module.mcp_tools()}
    assert {n for n, t in tools.items() if t.writes} == {"kb_write", "kb_move"}

    async def call(name: str, **args: object) -> object:
        t = tools[name]
        model: BaseModel = t.input_model(**args)
        return await t.handler(model, CallContext("s-1"))

    made = await call("kb_write", title="Tool Page", folder="Notes", body="x")
    assert isinstance(made, dict)
    read = await call("kb_read", id=made["id"])
    assert isinstance(read, dict) and read["body"] == "x"
    assert await call("kb_link", id=made["id"]) == {
        "id": made["id"],
        "title": "Tool Page",
        "path": "Notes/Tool Page.md",
    }
    moved = await call("kb_move", id=made["id"], folder="Archive")
    assert isinstance(moved, dict) and moved["page"]["path"] == "Archive/Tool Page.md"


async def test_conflicts_and_scan_routes(client: httpx.AsyncClient, kb_root: Path) -> None:
    (kb_root / "Inbox").mkdir()
    (kb_root / "Inbox/Plain.md").write_text("plain\n")
    report = (await client.post("/api/kb/scan")).json()
    assert report["adopted"] == ["Inbox/Plain.md"]
    assert (await client.get("/api/kb/conflicts")).json() == []


async def test_change_root(
    client: httpx.AsyncClient, kb_cfg: Config, module: KbModule, tmp_path: Path
) -> None:
    seen: list[Event] = []

    async def rec(e: Event) -> None:
        seen.append(e)

    ctx = module._ctx
    assert ctx is not None
    ctx.bus.subscribe("kb.root_changed", rec)
    assert (
        await client.put("/api/kb/path", json={"path": str(tmp_path / "nope")})
    ).status_code == 400
    assert (await client.put("/api/kb/path", json={"path": "relative"})).status_code == 400
    new = tmp_path / "kb2"
    new.mkdir()
    (new / "Plain.md").write_text("hi\n")
    r = (await client.put("/api/kb/path", json={"path": str(new)})).json()
    assert r["path"] == str(new) and r["adopted"] == ["Plain.md"]
    assert load_config(kb_cfg.data_dir / "config.toml").kb_path == new
    assert seen and seen[0].payload == {"path": str(new)}


async def test_health_reports_problem_without_kb(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [KbModule()])) as c:
        body = (await c.get("/healthz")).json()
        assert body["modules"]["kb"] == {"ok": False, "detail": "no KB path configured"}
        r = await c.get("/api/kb/tree", headers=bearer(cfg.token))
        assert r.status_code == 503


async def test_health_ok_with_kb(client: httpx.AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert (
        body["modules"]["kb"]["ok"] is True and "pages indexed" in body["modules"]["kb"]["detail"]
    )


async def test_service_before_start_is_503() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _ = KbModule().service


async def test_watcher_emits_external_change(
    client: httpx.AsyncClient, module: KbModule, kb_root: Path
) -> None:
    p = await _create(client, "Watched", "v1")
    seen: list[Event] = []

    async def rec(e: Event) -> None:
        seen.append(e)

    ctx = module._ctx
    assert ctx is not None
    ctx.bus.subscribe("kb.page_changed", rec)
    f = kb_root / "Notes/Watched.md"
    f.write_text(f.read_text().replace("v1", "edited elsewhere"))
    for _ in range(40):
        if any(e.payload.get("origin") == "external" for e in seen):
            break
        await asyncio.sleep(0.1)
    ext = [e for e in seen if e.payload.get("origin") == "external"]
    assert ext and ext[0].payload["id"] == p["id"] and ext[0].payload["change"] == "updated"
    assert module.watcher is not None and module.watcher.last_activity is not None


async def test_change_root_resets_index(client: httpx.AsyncClient, tmp_path: Path) -> None:
    await _create(client, "Old KB page")
    other = tmp_path / "other-kb"
    other.mkdir()
    r = await client.put("/api/kb/path", json={"path": str(other)})
    assert r.status_code == 200 and r.json()["missing"] == []
    assert (await client.get("/api/kb/conflicts")).json() == []
