# SPDX-License-Identifier: AGPL-3.0-or-later
"""The zotero module inside a running server."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from fastapi import FastAPI, HTTPException

from jesinia.apps.zotero.application.sync import SyncService
from jesinia.apps.zotero.infrastructure.local_api import LocalApiClient
from jesinia.apps.zotero.module import ZoteroModule
from jesinia.core.app import create_app
from jesinia.core.config import Config, ZoteroConfig
from jesinia.services.kb.module import KbModule
from jesinia.services.search.module import SearchModule
from jesinia.testing.fake_zotero import FakeZotero
from tests.apps.zotero.conftest import NoKeyStore
from tests.conftest import running


def build(cfg: Config, fake: FakeZotero) -> tuple[ZoteroModule, SearchModule, FastAPI]:
    zotero = ZoteroModule(
        library_factory=lambda config: LocalApiClient(
            config.zotero.base_url, NoKeyStore(), transport=fake.transport()
        )
    )
    search = SearchModule()
    app = create_app(cfg, [KbModule(), search, zotero], static_root=None)
    return zotero, search, app


async def test_should_register_papers_found_by_sync_now(cfg: Config) -> None:
    fake = FakeZotero()
    key = fake.add_item("Module Paper")
    zotero, search, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
        hits = await search.service.query("module", sources={"zotero"})
        assert [hit.key for hit in hits] == [key]
        assert zotero.health().ok and zotero.router() is None and len(zotero.mcp_tools()) == 3


async def test_should_reregister_all_papers_at_start(cfg: Config) -> None:
    fake = FakeZotero()
    key = fake.add_item("Persistent Paper")
    zotero, _, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
    for leftover in cfg.data_dir.glob("search.sqlite*"):
        leftover.unlink()
    fake.down = True  # so the start-up sync cannot be what restores the hit
    _, search, second = build(cfg, fake)
    async with running(second):
        hits = await search.service.query("persistent", sources={"zotero"})
        assert [hit.key for hit in hits] == [key]


async def test_should_stay_healthy_when_zotero_not_running(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeZotero()
    fake.down = True
    zotero, _, app = build(cfg, fake)
    async with running(app):
        with caplog.at_level(logging.WARNING):
            await zotero.sync_now()
        assert zotero.health().ok
        assert "not_running" in caplog.text


async def test_should_log_unexpected_sync_failures_with_their_cause(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeZotero()
    fake.mode = "broken"
    zotero, _, app = build(cfg, fake)
    async with running(app):
        with caplog.at_level(logging.WARNING):
            await zotero.sync_now()
        assert "boom" in caplog.text


async def test_should_use_the_default_client_without_a_key_when_zotero_is_absent(
    cfg: Config,
) -> None:
    unreachable = replace(cfg, zotero=ZoteroConfig(base_url="http://127.0.0.1:1"))
    zotero = ZoteroModule()
    app = create_app(unreachable, [KbModule(), SearchModule(), zotero], static_root=None)
    async with running(app):
        await zotero.sync_now()
        assert zotero.health().ok


async def test_should_report_not_started_before_start_and_after_stop(cfg: Config) -> None:
    zotero = ZoteroModule()
    with pytest.raises(HTTPException) as caught:
        _ = zotero.sync_service
    assert caught.value.status_code == 503 and "not started" in str(caught.value.detail)
    with pytest.raises(HTTPException):
        _ = zotero.mirror
    with pytest.raises(HTTPException):
        _ = zotero.library
    with pytest.raises(HTTPException):
        _ = zotero.search
    assert not zotero.health().ok
    await zotero.sync_now()  # a no-op, not an error
    fake = FakeZotero()
    zotero, _, app = build(cfg, fake)
    async with running(app):
        assert zotero.sync_service is not None and zotero.mirror is not None
        assert zotero.library is not None
    with pytest.raises(HTTPException):
        _ = zotero.sync_service


async def test_should_fail_start_without_the_search_service(cfg: Config) -> None:
    zotero = ZoteroModule()
    app = create_app(cfg, [zotero], static_root=None)
    async with running(app):
        assert not zotero.health().ok


async def test_should_start_even_when_reregistering_papers_fails(
    cfg: Config, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: SyncService) -> None:
        raise RuntimeError("search index unreadable")

    monkeypatch.setattr(SyncService, "reregister_all", broken)
    zotero, _, app = build(cfg, FakeZotero())
    with caplog.at_level(logging.WARNING):
        async with running(app):
            assert zotero.health().ok
    assert "search index unreadable" in caplog.text
