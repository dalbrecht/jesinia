# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/kb/assets/{page_id}/{file}``: a page's local images, and nothing else (R-REN2)."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from jesinia.core.app import create_app
from jesinia.core.config import Config, load_config
from jesinia.services.kb.module import KbModule
from tests.conftest import bearer, lifespan_client

PAGE = "0199e000-0000-7000-8000-000000000001"
OTHER = "0199e000-0000-7000-8000-000000000002"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x01" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    (root / "_assets" / PAGE).mkdir(parents=True)
    (root / "_assets" / PAGE / "a1b2.png").write_bytes(PNG)
    (root / "_assets" / PAGE / "photo.jpg").write_bytes(JPEG)
    return root


@pytest.fixture
async def anonymous(cfg: Config, kb_root: Path) -> AsyncIterator[httpx.AsyncClient]:
    load_config(cfg.data_dir / "config.toml")
    async with lifespan_client(create_app(replace(cfg, kb_path=kb_root), [KbModule()])) as c:
        yield c


@pytest.fixture
def client(anonymous: httpx.AsyncClient, cfg: Config) -> httpx.AsyncClient:
    anonymous.headers.update(bearer(cfg.token))
    return anonymous


async def test_should_serve_the_image_bytes_with_their_sniffed_type(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get(f"/api/kb/assets/{PAGE}/a1b2.png")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]
    assert r.headers["cache-control"].startswith("private")
    jpeg = await client.get(f"/api/kb/assets/{PAGE}/photo.jpg")
    assert jpeg.content == JPEG and jpeg.headers["content-type"] == "image/jpeg"


async def test_should_type_by_content_not_by_extension(
    client: httpx.AsyncClient, kb_root: Path
) -> None:
    (kb_root / "_assets" / PAGE / "really-a-png.gif").write_bytes(PNG)
    r = await client.get(f"/api/kb/assets/{PAGE}/really-a-png.gif")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


async def test_should_require_auth(anonymous: httpx.AsyncClient) -> None:
    r = await anonymous.get(f"/api/kb/assets/{PAGE}/a1b2.png")
    assert r.status_code == 401
    assert PNG not in r.content


@pytest.mark.parametrize(
    "path",
    [
        f"/api/kb/assets/{PAGE}/..",
        f"/api/kb/assets/{PAGE}/%2e%2e",
        f"/api/kb/assets/{PAGE}/..%2F..%2Fsecret.png",
        f"/api/kb/assets/{PAGE}/%2E%2E%2Fsecret.png",
        f"/api/kb/assets/{PAGE}/.gitignore",
        f"/api/kb/assets/{PAGE}/a%20b.png",
        "/api/kb/assets/../Notes/secret.png",
        "/api/kb/assets/..%2FNotes/secret.png",
        "/api/kb/assets/not-a-page-id/a1b2.png",
        f"/api/kb/assets/{PAGE.upper()}/a1b2.png",
    ],
)
async def test_should_refuse_traversal_and_unsafe_names(
    client: httpx.AsyncClient, kb_root: Path, path: str
) -> None:
    (kb_root / "Notes").mkdir()
    (kb_root / "Notes" / "secret.png").write_bytes(PNG)
    (kb_root / "_assets" / PAGE / ".gitignore").write_bytes(b"*\n")
    r = await client.get(path)
    assert r.status_code in (400, 404), r.status_code
    assert PNG not in r.content


async def test_should_refuse_a_symlink_that_escapes(
    client: httpx.AsyncClient, kb_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    os.symlink(outside, kb_root / "_assets" / PAGE / "escape.png")
    r = await client.get(f"/api/kb/assets/{PAGE}/escape.png")
    assert r.status_code in (400, 404)
    assert PNG not in r.content


async def test_should_refuse_a_symlink_even_inside_the_kb(
    client: httpx.AsyncClient, kb_root: Path
) -> None:
    os.symlink(kb_root / "_assets" / PAGE / "a1b2.png", kb_root / "_assets" / PAGE / "alias.png")
    assert (await client.get(f"/api/kb/assets/{PAGE}/alias.png")).status_code in (400, 404)


async def test_should_refuse_a_symlinked_page_folder(
    client: httpx.AsyncClient, kb_root: Path
) -> None:
    os.symlink(kb_root / "_assets" / PAGE, kb_root / "_assets" / OTHER)
    assert (await client.get(f"/api/kb/assets/{OTHER}/a1b2.png")).status_code in (400, 404)


async def test_should_refuse_a_symlinked_assets_folder(
    client: httpx.AsyncClient, kb_root: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / PAGE).mkdir(parents=True)
    (elsewhere / PAGE / "x.png").write_bytes(PNG)
    os.rename(kb_root / "_assets", kb_root / "_assets_old")
    os.symlink(elsewhere, kb_root / "_assets")
    r = await client.get(f"/api/kb/assets/{PAGE}/x.png")
    assert r.status_code in (400, 404) and PNG not in r.content


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"<!doctype html><script>alert(1)</script>",
        b"plain text",
    ],
)
async def test_should_refuse_a_file_that_is_not_a_raster_image(
    client: httpx.AsyncClient, kb_root: Path, data: bytes
) -> None:
    (kb_root / "_assets" / PAGE / "bad.png").write_bytes(data)
    r = await client.get(f"/api/kb/assets/{PAGE}/bad.png")
    assert r.status_code == 415
    assert r.json()["detail"].startswith("UnsupportedAssetType")
    assert data not in r.content


async def test_should_answer_404_for_a_missing_file_or_folder(
    client: httpx.AsyncClient, kb_root: Path
) -> None:
    missing = await client.get(f"/api/kb/assets/{PAGE}/nope.png")
    assert missing.status_code == 404
    assert missing.json()["detail"].startswith("AssetNotFound")
    assert (await client.get(f"/api/kb/assets/{OTHER}/a1b2.png")).status_code == 404
    (kb_root / "_assets" / PAGE / "dir.png").mkdir()
    assert (await client.get(f"/api/kb/assets/{PAGE}/dir.png")).status_code == 404


async def test_should_answer_503_without_a_kb(cfg: Config) -> None:
    load_config(cfg.data_dir / "config.toml")
    async with lifespan_client(create_app(replace(cfg, kb_path=None), [KbModule()])) as c:
        r = await c.get(f"/api/kb/assets/{PAGE}/a1b2.png", headers=bearer(cfg.token))
    assert r.status_code == 503


async def test_should_answer_404_for_a_fifo_promptly(
    client: httpx.AsyncClient, kb_root: Path
) -> None:
    os.mkfifo(kb_root / "_assets" / PAGE / "pipe.png")
    r = await asyncio.wait_for(client.get(f"/api/kb/assets/{PAGE}/pipe.png"), timeout=5)
    assert r.status_code == 404
