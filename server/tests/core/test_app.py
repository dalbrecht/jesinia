# SPDX-License-Identifier: AGPL-3.0-or-later
from collections.abc import Sequence

import pytest
from fastapi import APIRouter
from pydantic import BaseModel

import jesinia
from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.module import AppContext, CallContext, ModuleHealth, ToolDef
from tests.conftest import bearer, lifespan_client


class Healthy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = False
        self.stopped = False
        self.order: list[str] | None = None

    def router(self) -> APIRouter | None:
        r = APIRouter()

        @r.get(f"/api/{self.name}/hello")
        async def hello() -> dict[str, str]:
            return {"hello": self.name}

        return r

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        self.started = True
        ctx.services[self.name] = self
        if self.order is not None:
            self.order.append(f"start:{self.name}")

    async def stop(self) -> None:
        self.stopped = True
        if self.order is not None:
            self.order.append(f"stop:{self.name}")

    def health(self) -> ModuleHealth:
        return ModuleHealth(True, "ready")


class Exploding(Healthy):
    async def start(self, ctx: AppContext) -> None:
        raise RuntimeError("cannot start")

    def router(self) -> APIRouter | None:
        return None


def test_app_reports_the_package_version(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jesinia, "__version__", "9.9.9")
    assert create_app(cfg, []).version == "9.9.9"


async def test_failing_module_does_not_block_others(cfg: Config) -> None:
    a, b = Exploding("a"), Healthy("b")
    app = create_app(cfg, [a, b])
    async with lifespan_client(app) as c:
        body = (await c.get("/healthz")).json()
    assert body["ok"] is False
    assert body["modules"]["a"] == {"ok": False, "detail": "start failed: cannot start"}
    assert body["modules"]["b"] == {"ok": True, "detail": "ready"}
    assert b.stopped and not a.stopped


async def test_modules_start_in_order_and_stop_in_reverse(cfg: Config) -> None:
    order: list[str] = []
    mods = [Healthy("x"), Healthy("y")]
    for m in mods:
        m.order = order
    async with lifespan_client(create_app(cfg, mods)) as c:
        assert (await c.get("/healthz")).json()["ok"] is True
    assert order == ["start:x", "start:y", "stop:y", "stop:x"]


async def test_module_routes_require_auth(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [Healthy("m")])) as c:
        assert (await c.get("/api/m/hello")).status_code == 401
        r = await c.get("/api/m/hello", headers=bearer(cfg.token))
        assert r.status_code == 200 and r.json() == {"hello": "m"}


async def test_module_routes_refuse_agent_session_tokens(cfg: Config) -> None:
    from jesinia.core.auth import register_session_token

    register_session_token("agent-token", "s-1")
    async with lifespan_client(create_app(cfg, [Healthy("m")])) as c:
        assert (await c.get("/api/m/hello", headers=bearer("agent-token"))).status_code == 403


async def test_context_exposes_services(cfg: Config) -> None:
    m = Healthy("svc")
    app = create_app(cfg, [m])
    async with lifespan_client(app):
        ctx: AppContext = app.state.ctx
        assert ctx.services["svc"] is m and ctx.cfg is cfg


def test_tooldef_shape() -> None:
    class In(BaseModel):
        q: str

    async def handler(args: BaseModel, call: CallContext) -> object:
        return {"q": getattr(args, "q", None), "session": call.session_id}

    t = ToolDef("kb_search", "Search the KB", In, handler, writes=False)
    assert t.name == "kb_search" and not t.writes


@pytest.mark.parametrize("bad", ["kb.search", "x" * 65, "has space"])
def test_tooldef_rejects_bad_wire_names(bad: str) -> None:
    class In(BaseModel):
        pass

    async def handler(args: BaseModel, call: CallContext) -> object:
        return None

    with pytest.raises(ValueError):
        ToolDef(bad, "d", In, handler, writes=False)


class StopFails(Healthy):
    async def stop(self) -> None:
        raise RuntimeError("stop failed")


async def test_stop_failure_is_logged_and_others_still_stop(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    bad, good = StopFails("bad"), Healthy("good")
    async with lifespan_client(create_app(cfg, [good, bad])):
        pass
    assert good.stopped and "failed to stop" in caplog.text


class ExplodingWithRoutes(Healthy):
    async def start(self, ctx: AppContext) -> None:
        raise RuntimeError("db locked")


async def test_failed_module_routes_answer_503(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [ExplodingWithRoutes("bad"), Healthy("ok")])) as c:
        r = await c.get("/api/bad/hello", headers=bearer(cfg.token))
        assert r.status_code == 503 and "db locked" in r.json()["detail"]
        assert (await c.get("/api/ok/hello", headers=bearer(cfg.token))).status_code == 200
