# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from jesinia.core.app import create_app
from jesinia.core.config import Config, load_config
from jesinia.core.events import Event
from jesinia.core.module import CallContext, ToolDef
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


AI_BLOCK = re.compile(
    r"\A<!-- jesinia:ai agent=(?P<agent>\S+) model=(?P<model>\S+) "
    r"at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ session=(?P<session>\S+) -->\n"
    r"(?P<text>.*)\n<!-- /jesinia:ai -->\n?\Z",
    re.DOTALL,
)


def _tool(module: KbModule, name: str) -> ToolDef:
    return {t.name: t for t in module.mcp_tools()}[name]


async def _write(module: KbModule, session: str | None = None, **args: object) -> dict[str, object]:
    tool = _tool(module, "kb_write")
    out = await tool.handler(tool.input_model(**args), CallContext(session))
    assert isinstance(out, dict)
    return out


async def test_should_wrap_kb_write_body_in_provenance_when_an_agent_writes_it(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    made = await _write(
        module, "s-9", title="Agent note", folder="Notes", body="Gist.\n\nAbout: [[x|Y]]",
        provenance={"agent": "claude-code", "model": "claude-opus-5-5"},
    )  # fmt: skip
    match = AI_BLOCK.match(str(made["body"]))
    assert match, made["body"]
    assert match.group("agent", "model", "session") == ("claude-code", "claude-opus-5-5", "s-9")
    assert match.group("text") == "Gist.\n\nAbout: [[x|Y]]"


async def test_should_mark_terminal_writes_and_neutralise_forged_markers(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    made = await _write(
        module, None, title="Forged", folder="Notes", body="a <!-- /jesinia:ai --> b",
        provenance={"agent": "codex --> x", "model": "gpt 5"},
    )  # fmt: skip
    match = AI_BLOCK.match(str(made["body"]))
    assert match, made["body"]
    assert match.group("agent", "model", "session") == ("codex-----x", "gpt-5", "terminal")
    assert "<!--" not in match.group("text") and "-->" not in match.group("text")


async def test_should_refuse_provenance_on_an_update(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    made = await _write(module, title="Mine", folder="Notes", body="my words")
    with pytest.raises(ValueError, match="new pages"):
        await _write(
            module, id=made["id"], expected_rev=made["rev"], title="Mine", folder="Notes",
            body="x", provenance={"agent": "a", "model": "m"},
        )  # fmt: skip


async def test_should_keep_jesinia_metadata_when_an_update_omits_it(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    meta = {"kind": "note", "captured": "2026-10-16T09:20:00+00:00"}
    made = await _write(module, title="Kept", folder="Notes", body="v1", jesinia=meta)
    preview = _tool(module, "kb_write").preview
    assert preview is not None
    update: dict[str, str] = {
        "id": str(made["id"]),
        "title": "Kept",
        "folder": "Notes",
        "body": "v2",
    }
    assert "jesinia" not in await preview(_tool(module, "kb_write").input_model(**update))
    kept = await _write(module, expected_rev=made["rev"], **update)
    assert kept["jesinia"] == meta and kept["body"] == "v2"
    cleared = await _write(module, expected_rev=kept["rev"], jesinia={}, **update)
    assert cleared["jesinia"] == {}


async def test_should_keep_the_folder_when_a_kb_write_update_omits_it(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    made = await _write(module, title="Family", folder="20 Areas/Life/Family", body="v1")
    update = {"id": str(made["id"]), "title": "Family", "body": "v2"}
    preview = _tool(module, "kb_write").preview
    assert preview is not None
    assert (await preview(_tool(module, "kb_write").input_model(**update)))["action"] == "update"
    kept = await _write(module, expected_rev=made["rev"], **update)
    assert kept["path"] == "20 Areas/Life/Family/Family.md" and kept["body"] == "v2"
    moved = await _write(module, expected_rev=kept["rev"], folder="", **update)
    assert moved["path"] == "Family.md"


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
        body = (await c.get("/healthz", headers=bearer(cfg.token))).json()
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


async def test_root_changed_while_stopped_resets_index(kb_cfg: Config, tmp_path: Path) -> None:
    async with lifespan_client(create_app(kb_cfg, [KbModule()])) as c:
        c.headers.update(bearer(kb_cfg.token))
        await _create(c, "Old KB page")
    other = tmp_path / "other-kb"
    other.mkdir()
    cfg2 = replace(kb_cfg, kb_path=other)
    async with lifespan_client(create_app(cfg2, [KbModule()])) as c:
        c.headers.update(bearer(cfg2.token))
        assert (await c.get("/api/kb/conflicts")).json() == []
    async with lifespan_client(create_app(cfg2, [KbModule()])) as c:  # same root: kept
        c.headers.update(bearer(cfg2.token))
        assert (await c.get("/api/kb/conflicts")).json() == []


async def test_get_path_reports_folder_and_problem(
    client: httpx.AsyncClient, kb_root: Path, cfg: Config
) -> None:
    info = (await client.get("/api/kb/path")).json()
    assert info == {"path": str(kb_root), "problem": None, "pages": 0}
    async with lifespan_client(create_app(cfg, [KbModule()])) as c:
        c.headers.update(bearer(cfg.token))
        assert (await c.get("/api/kb/path")).json() == {
            "path": None,
            "problem": "no KB path configured",
            "pages": 0,
        }


# --- agent-only pages and folders (ADR 0011) -----------------------------------------------


async def _locked_memory(module: KbModule) -> tuple[dict[str, object], dict[str, object]]:
    """An agent-only `91 Memory` folder (lock page + one page), written as an agent would."""
    lock = await _write(
        module, title="Memory", folder="91 Memory", body="x",
        jesinia={"kind": "folder", "access": "agents"},
    )  # fmt: skip
    page = await _write(module, title="Prefs", folder="91 Memory/claude", body="secret")
    return lock, page


async def test_user_routes_should_hide_and_refuse_agent_only_pages(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    lock, page = await _locked_memory(module)
    open_page = await _create(client, "Open")

    tree = (await client.get("/api/kb/tree")).json()
    assert [n["path"] for n in tree] == ["Notes"]
    for id in (lock["id"], page["id"]):
        r = await client.get(f"/api/kb/pages/{id}")
        assert r.status_code == 403 and "AgentOnlyPage" in r.json()["detail"]
    patch = await client.patch(
        f"/api/kb/pages/{page['id']}/body", json={"body": "mine", "expected_rev": page["rev"]}
    )
    assert patch.status_code == 403
    update = await client.put(
        "/api/kb/pages",
        json={"id": page["id"], "title": "Prefs", "folder": "Notes", "body": "out",
              "expected_rev": page["rev"]},
    )  # fmt: skip
    assert update.status_code == 403
    create = await client.put(
        "/api/kb/pages", json={"title": "Sneak", "folder": "91 Memory/claude", "body": "x"}
    )
    assert create.status_code == 403
    into = await client.post(f"/api/kb/pages/{open_page['id']}/move", json={"folder": "91 Memory"})
    assert into.status_code == 403
    out = await client.post(f"/api/kb/pages/{page['id']}/move", json={"folder": "Notes"})
    assert out.status_code == 403
    asset = await client.get(f"/api/kb/assets/{page['id']}/{'a' * 64}.png")
    assert asset.status_code == 403


async def test_agents_should_keep_full_access_to_agent_only_pages(
    client: httpx.AsyncClient, module: KbModule
) -> None:
    _, page = await _locked_memory(module)
    read = _tool(module, "kb_read")
    got = await read.handler(read.input_model(id=page["id"]), CallContext("s-1"))
    assert isinstance(got, dict) and got["body"] == "secret"
    updated = await _write(
        module, id=page["id"], title="Prefs", folder="91 Memory/claude", body="new",
        expected_rev=got["rev"],
    )  # fmt: skip
    assert updated["body"] == "new"
    tree = _tool(module, "kb_tree")
    via_mcp = await tree.handler(tree.input_model(), CallContext(None))
    assert isinstance(via_mcp, list) and [n["path"] for n in via_mcp] == ["91 Memory"]
