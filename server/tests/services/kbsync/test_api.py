# SPDX-License-Identifier: AGPL-3.0-or-later
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from jesinia.core.app import create_app
from jesinia.core.config import Config, load_config
from jesinia.core.module import CallContext
from jesinia.services.kb.module import KbModule
from jesinia.services.kbsync.module import KbSyncModule
from tests.conftest import bearer, lifespan_client
from tests.services.kbsync.conftest import git

CfgFor = Callable[[Path | None], Config]


@pytest.fixture
def cfg_for(cfg: Config) -> CfgFor:
    load_config(cfg.data_dir / "config.toml")

    def make(root: Path | None) -> Config:
        # A short quiet period: the watcher sees test edits, and the cycle waits it out.
        return replace(cfg, kb_path=root, kbsync_quiet=timedelta(milliseconds=200))

    return make


async def test_status_run_and_tools(cfg_for: CfgFor, repo_with_remote: Path, remote: Path) -> None:
    sync = KbSyncModule()
    cfg = cfg_for(repo_with_remote)
    async with lifespan_client(create_app(cfg, [KbModule(), sync])) as c:
        c.headers.update(bearer(cfg.token))
        st = (await c.get("/api/kbsync/status")).json()
        assert st["state"]["mode"] == "push" and st["paused"] is False
        (repo_with_remote / "Notes/a.md").write_text("changed by user\n")
        r = (await c.post("/api/kbsync/run")).json()
        assert r["result"]["outcome"] == "pushed" and r["status"]["last_run"] is not None
        assert (await c.post("/api/kbsync/resume")).json()["paused"] is False
        assert (await c.post("/api/kbsync/init")).status_code == 400  # already a repo
        assert (await c.get("/healthz")).json()["modules"]["kbsync"]["ok"] is True
        tools = {t.name: t for t in sync.mcp_tools()}
        assert tools["kb_sync_now"].writes and not tools["kb_sync_status"].writes
        s = await tools["kb_sync_status"].handler(
            tools["kb_sync_status"].input_model(), CallContext(None)
        )
        assert isinstance(s, dict) and s["state"]["mode"] == "push"
        n = await tools["kb_sync_now"].handler(
            tools["kb_sync_now"].input_model(), CallContext(None)
        )
        # The kb module may still be adopting/writing files, so either outcome is valid.
        assert isinstance(n, dict) and n["outcome"] in {"skipped", "pushed"}
    assert "Jesinia-Auto-Sync" in git(remote, "log", "-1", "--format=%B", "main")


async def test_shutdown_runs_a_final_cycle(
    cfg_for: CfgFor, repo_with_remote: Path, remote: Path
) -> None:
    async with lifespan_client(create_app(cfg_for(repo_with_remote), [KbModule(), KbSyncModule()])):
        (repo_with_remote / "Notes/late.md").write_text("written just before quitting\n")
    assert "late.md" in git(remote, "show", "--name-only", "--format=", "main")


async def test_off_and_init(cfg_for: CfgFor, tmp_path: Path) -> None:
    root = tmp_path / "plainkb"
    root.mkdir()
    cfg = cfg_for(root)
    async with lifespan_client(create_app(cfg, [KbModule(), KbSyncModule()])) as c:
        c.headers.update(bearer(cfg.token))
        assert (await c.get("/healthz")).json()["modules"]["kbsync"] == {
            "ok": True,
            "detail": "sync off",
        }
        st = (await c.post("/api/kbsync/init")).json()
        assert st["state"]["mode"] == "commit" and (root / ".git").is_dir()
        assert (await c.get("/healthz")).json()["modules"]["kbsync"] == {
            "ok": True,
            "detail": "mode commit",
        }


async def test_root_change_redetects(cfg_for: CfgFor, tmp_path: Path, repo_no_remote: Path) -> None:
    plain = tmp_path / "plain2"
    plain.mkdir()
    cfg = cfg_for(plain)
    async with lifespan_client(create_app(cfg, [KbModule(), KbSyncModule()])) as c:
        c.headers.update(bearer(cfg.token))
        assert (await c.get("/api/kbsync/status")).json()["state"]["mode"] == "off"
        assert (await c.put("/api/kb/path", json={"path": str(repo_no_remote)})).status_code == 200
        r = await c.get("/api/kbsync/status")
        assert r.status_code == 200, r.text
        assert r.json()["state"]["mode"] == "commit"


async def test_health_states(cfg_for: CfgFor, repo_with_remote: Path) -> None:
    mod = KbSyncModule()
    assert mod.health().ok is False
    with pytest.raises(Exception):
        _ = mod.service
    git(repo_with_remote, "branch", "--unset-upstream")
    cfg = cfg_for(repo_with_remote)
    async with lifespan_client(create_app(cfg, [KbModule(), mod])) as c:
        c.headers.update(bearer(cfg.token))
        h = (await c.get("/healthz")).json()["modules"]["kbsync"]
        assert h["ok"] is True and "upstream" in h["detail"]
    git(repo_with_remote, "checkout", "-q", "--detach")
    async with lifespan_client(create_app(cfg, [KbModule(), KbSyncModule()])) as c:
        c.headers.update(bearer(cfg.token))
        h = (await c.get("/healthz")).json()["modules"]["kbsync"]
        assert h["ok"] is False and "Detached HEAD" in h["detail"]


async def test_paused_health(cfg_for: CfgFor, repo_with_remote: Path) -> None:
    mod = KbSyncModule()
    cfg = cfg_for(repo_with_remote)
    async with lifespan_client(create_app(cfg, [KbModule(), mod])) as c:
        c.headers.update(bearer(cfg.token))
        mod.service._paused = True
        h = (await c.get("/healthz")).json()["modules"]["kbsync"]
        assert h == {"ok": False, "detail": "sync paused: merge conflict"}
        mod.service._paused = False


async def test_without_kb_module_and_failed_shutdown(
    cfg_for: CfgFor,
    repo_with_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from jesinia.services.kbsync.application.service import SyncService

    async def boom(self: SyncService, trigger: str) -> None:
        raise RuntimeError("network down")

    cfg = cfg_for(repo_with_remote)
    async with lifespan_client(create_app(cfg, [KbSyncModule()])) as c:
        c.headers.update(bearer(cfg.token))
        assert (await c.get("/healthz")).json()["modules"]["kbsync"]["ok"] is True
        monkeypatch.setattr(SyncService, "run_cycle", boom)
    assert "final KB sync at shutdown failed" in caplog.text
