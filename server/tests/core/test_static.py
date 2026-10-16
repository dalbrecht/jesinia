# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import pytest

from jesinia.core.app import create_app
from jesinia.core.config import Config
from tests.conftest import lifespan_client


@pytest.fixture
def static(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    (root / "launcher/assets").mkdir(parents=True)
    (root / "launcher/index.html").write_text("<p>launcher</p>")
    (root / "launcher/manifest.webmanifest").write_text("{}")
    (root / "launcher/assets/app-abc.js").write_text("js")
    (root / "notes").mkdir()
    (root / "notes/index.html").write_text("<p>notes</p>")
    (tmp_path / "secret.txt").write_text("nope")
    return root


async def test_serves_launcher_apps_and_spa_fallback(cfg: Config, static: Path) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=static)) as c:
        assert (await c.get("/")).text == "<p>launcher</p>"
        assert (await c.get("/settings/")).text == "<p>launcher</p>"
        asset = await c.get("/assets/app-abc.js")
        assert asset.text == "js" and "immutable" in asset.headers["cache-control"]
        assert (await c.get("/")).headers["cache-control"] == "no-cache"
        manifest = await c.get("/manifest.webmanifest")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert (await c.get("/notes/")).text == "<p>notes</p>"
        assert (await c.get("/notes/page/123")).text == "<p>notes</p>"  # client route
        assert (await c.get("/notes/missing.js")).status_code == 404
        assert (await c.get("/assets/missing.css")).status_code == 404
        assert (await c.get("/notes/../../secret.txt")).status_code == 404
        assert (await c.get("/notes/%2e%2e/%2e%2e/secret.txt")).status_code == 404
        assert (await c.get("/%2e%2e/secret.txt")).status_code == 404
        assert (await c.get("/%2e%2e/static/notes/index.html")).status_code == 404
        assert (await c.get("/%2e/notes/index.html")).status_code == 404
        assert (await c.get("/api/nothing")).status_code == 404
        assert (await c.get("/healthz")).json()["ok"] is True  # real routes still win
        assert (await c.get("/openapi.json")).status_code == 200


async def test_unbuilt_app_explains(cfg: Config, tmp_path: Path) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=tmp_path / "empty")) as c:
        r = await c.get("/")
        assert r.status_code == 404 and "make build" in r.text
