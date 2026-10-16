# SPDX-License-Identifier: AGPL-3.0-or-later
import plistlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jesinia.cli import install as inst
from jesinia.cli import main
from jesinia.cli.install import System, render_plist
from jesinia.core.config import load_config

HEALTHY_MODULES: dict[str, object] = {
    name: {"ok": True, "detail": "ok"} for name in inst.required_modules()
}
HEALTHY = {"ok": True, "modules": HEALTHY_MODULES}


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JESINIA_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return home


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    d = tmp_path / "kb"
    d.mkdir()
    (d / "note.md").write_text("mine")
    return d


@dataclass
class Spy(System):
    calls: list[list[str]] = field(default_factory=list)
    out: list[str] = field(default_factory=list)
    fail: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.echo = self.out.append
        self.which = lambda n: f"/bin/{n}"
        self.environ = {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/ssh", "CODEX_HOME": ""}

    def run(self, argv: list[str], *, check: bool = True) -> tuple[int, str]:
        if self.dry_run:
            return super().run(argv, check=check)
        self.calls.append(argv)
        failed = any(f in " ".join(argv) for f in self.fail)
        if failed and check:
            raise RuntimeError("failed")
        return (1 if failed else 0), ""


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst, "_wait_healthy", lambda sys, cfg: True)
    monkeypatch.setattr(
        inst, "call", lambda cfg, m, p, **k: {"url": f"http://127.0.0.1:{cfg.port}/login?nonce=n"}
    )


def test_plist_is_valid_and_keepalive_on_crash_only(tmp_path: Path) -> None:
    p = plistlib.loads(
        render_plist(Path("/opt/uv"), Path("/r"), tmp_path, "/usr/bin", "/tmp/ssh").encode()
    )
    assert p["Label"] == "com.jesinia.server" and p["KeepAlive"] == {"SuccessfulExit": False}
    assert p["EnvironmentVariables"]["SSH_AUTH_SOCK"] == "/tmp/ssh"
    assert p["ProgramArguments"] == ["/opt/uv", "run", "--project", "/r/server", "jesinia", "serve"]
    assert p["RunAtLoad"] is True and p["StandardErrorPath"].startswith(str(tmp_path))
    bare = plistlib.loads(render_plist(Path("/u"), Path("/r"), tmp_path, "/b", None).encode())
    assert "SSH_AUTH_SOCK" not in bare["EnvironmentVariables"]


def test_dry_run_changes_nothing(tmp_home: Path, kb: Path) -> None:
    r = CliRunner().invoke(main.app, ["install", "--kb-path", str(kb / "new"), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "launchctl bootstrap" in r.stdout and "would create" in r.stdout
    assert "Bearer ***" in r.stdout or "claude" not in r.stdout
    assert not (tmp_home / "Library").exists() and not (kb / "new").exists()


def test_install_then_idempotent(tmp_home: Path, kb: Path, healthy: None) -> None:
    spy = Spy(yes=True)
    assert inst.install(kb, spy) == 0
    plist = tmp_home / "Library/LaunchAgents/com.jesinia.server.plist"
    env = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
    assert env["HOME"] == str(tmp_home) and env["SSH_AUTH_SOCK"] == "/tmp/ssh"
    cfg_path = tmp_home / "Library/Application Support/jesinia/config.toml"
    token = load_config(cfg_path).token
    assert load_config(cfg_path).kb_path == kb.resolve()
    first = [c[:2] for c in spy.calls if c[0] == "launchctl"]
    assert first == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    assert any(c[:3] == ["/bin/claude", "mcp", "add"] for c in spy.calls)
    codex = tomllib.loads((tmp_home / ".codex/config.toml").read_text())
    assert codex["mcp_servers"]["jesinia"]["http_headers"]["Authorization"] == f"Bearer {token}"
    assert spy.out[-1].endswith("Open: http://127.0.0.1:4870/login?nonce=n")

    spy2 = Spy(yes=True)
    assert inst.install(kb, spy2) == 0
    assert [c[:2] for c in spy2.calls if c[0] == "launchctl"] == first
    assert load_config(cfg_path).token == token


def test_install_asks_before_registering(tmp_home: Path, kb: Path, healthy: None) -> None:
    spy = Spy(yes=False)
    spy.confirm = lambda q: "Claude" in q
    assert inst.install(kb, spy) == 0
    assert any(c[0] == "/bin/claude" for c in spy.calls)
    assert not any(c[0] == "/bin/codex" for c in spy.calls)


def test_install_failures(tmp_home: Path, kb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(yes=True)
    spy.which = lambda n: None
    assert inst.install(kb, spy) == 1 and "uv is not on PATH" in spy.out[0]
    monkeypatch.setattr(inst, "_wait_healthy", lambda sys, cfg: False)
    spy = Spy(yes=True)
    assert inst.install(kb, spy) == 1 and "did not become healthy" in spy.out[-1]


def test_wait_healthy_times_out(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import respx
    from httpx import ConnectError, Response

    cfg = load_config(tmp_home / "c/config.toml")
    monkeypatch.setattr(inst, "HEALTH_TIMEOUT", 0.0)
    with respx.mock:
        respx.get("http://127.0.0.1:4870/healthz").mock(side_effect=ConnectError("x"))
        assert inst._wait_healthy(Spy(), cfg) is False
        respx.get("http://127.0.0.1:4870/healthz").mock(return_value=Response(200, json=HEALTHY))
        assert inst._wait_healthy(Spy(), cfg) is True
    monkeypatch.setattr(inst, "HEALTH_TIMEOUT", 5.0)
    slept: list[float] = []
    spy = Spy()
    spy.sleep = slept.append
    with respx.mock:
        respx.get("http://127.0.0.1:4870/healthz").mock(
            side_effect=[ConnectError("x"), Response(200, json=HEALTHY)]
        )
        assert inst._wait_healthy(spy, cfg) is True and slept == [0.5]


def test_uninstall_never_touches_kb(tmp_home: Path, kb: Path, healthy: None) -> None:
    inst.install(kb, Spy(yes=True))
    spy = Spy(yes=True)
    assert inst.uninstall(spy, purge_data=False) == 0
    assert not (tmp_home / "Library/LaunchAgents/com.jesinia.server.plist").exists()
    assert (tmp_home / "Library/Application Support/jesinia/config.toml").exists()
    assert ["/bin/claude", "mcp", "remove", "--scope", "user", "jesinia"] in spy.calls
    assert inst.uninstall(Spy(yes=True), purge_data=True) == 0
    assert not (tmp_home / "Library/Application Support/jesinia").exists()
    assert not (tmp_home / "Library/Logs/jesinia").exists()
    assert (kb / "note.md").read_text() == "mine"


def test_uninstall_purge_declined_or_overlapping(tmp_home: Path, healthy: None) -> None:
    data = tmp_home / "Library/Application Support/jesinia"
    inside = data / "kb"
    inside.mkdir(parents=True)
    inst.install(inside, Spy(yes=True))
    spy = Spy(yes=True)
    assert inst.uninstall(spy, purge_data=True) == 1 and "overlaps" in spy.out[-1]
    assert inside.exists()
    (data / "config.toml").unlink()
    no = Spy(yes=False)
    no.confirm = lambda q: False
    assert inst.uninstall(no, purge_data=True) == 0 and no.out[-1] == "kept data"
    assert data.exists()


def test_uninstall_dry_run(tmp_home: Path, kb: Path, healthy: None) -> None:
    inst.install(kb, Spy(yes=True))
    r = CliRunner().invoke(main.app, ["uninstall", "--dry-run", "--purge-data"])
    assert r.exit_code == 0 and "would remove" in r.stdout and "launchctl bootout" in r.stdout
    assert (tmp_home / "Library/LaunchAgents/com.jesinia.server.plist").exists()
    assert (tmp_home / "Library/Application Support/jesinia").exists()


def test_refresh_registrations(tmp_home: Path) -> None:
    cfg = load_config(tmp_home / "c/config.toml")
    spy = Spy(fail=("codex mcp get",))
    inst.refresh_registrations(cfg, spy)
    assert spy.out == ["Claude Code: updated"]
    assert not any(c[:3] == ["/bin/codex", "mcp", "add"] for c in spy.calls)
    spy2 = Spy()
    inst.refresh_registrations(cfg, spy2)
    assert spy2.out == ["Claude Code: updated", "Codex: updated"]


def test_register_codex_into_dir_and_dry_run(tmp_home: Path) -> None:
    cfg = load_config(tmp_home / "c/config.toml")
    spy = Spy()
    spy.environ = {"CODEX_HOME": str(tmp_home / "cx")}
    (tmp_home / "cx").mkdir()
    inst.register_codex(spy, cfg, "/bin/codex")
    text = (tmp_home / "cx/config.toml").read_text()
    assert f'Authorization = "Bearer {cfg.token}"' in text
    assert (tmp_home / "cx/config.toml").stat().st_mode & 0o777 == 0o600
    dry = Spy(dry_run=True)
    inst.register_codex(dry, cfg, "/bin/codex")
    assert cfg.token not in "\n".join(dry.out)


def test_real_system_run_and_files(tmp_path: Path) -> None:
    sys = System(echo=lambda s: None)
    assert sys.run(["python3", "-c", "print('ok')"]) == (0, "ok")
    with pytest.raises(RuntimeError, match="failed"):
        sys.run(["python3", "-c", "import sys; sys.exit(3)"])
    f = tmp_path / "d/f.txt"
    sys.write(f, "x", 0o600)
    assert f.read_text() == "x"
    sys.remove(f)
    sys.remove(tmp_path / "d")
    assert not (tmp_path / "d").exists()
    assert System(yes=False, confirm=lambda q: True).ask("?") is True


def test_private_write_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    def boom(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        inst._write_private(tmp_path / "config.toml", "secret")
    assert list(tmp_path.iterdir()) == []


def test_degraded_server_is_not_healthy(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import respx
    from httpx import Response

    cfg = load_config(tmp_home / "c/config.toml")
    monkeypatch.setattr(inst, "HEALTH_TIMEOUT", 0.0)
    degraded = {
        "ok": False,
        "modules": {
            "kb": {"ok": False, "detail": "migration failed"},
            "agents": {"ok": False, "detail": "x"},
        },
    }
    optional_only = {
        "ok": False,
        "modules": {**HEALTHY_MODULES, "agents": {"ok": False, "detail": "x"}},
    }
    with respx.mock:
        respx.get("http://127.0.0.1:4870/healthz").mock(return_value=Response(200, json=degraded))
        assert inst._wait_healthy(Spy(), cfg) is False
        respx.get("http://127.0.0.1:4870/healthz").mock(
            return_value=Response(200, json=optional_only)
        )
        assert inst._wait_healthy(Spy(), cfg) is True
        respx.get("http://127.0.0.1:4870/healthz").mock(return_value=Response(200, text="not json"))
        assert inst._wait_healthy(Spy(), cfg) is False
    assert inst.unhealthy_modules([]) == ["?"]


def test_missing_required_modules_are_unhealthy() -> None:
    required = frozenset({"kb", "search"})
    assert inst.unhealthy_modules({"ok": True, "modules": {}}, required) == ["kb", "search"]
    assert inst.unhealthy_modules({"modules": {"kb": {"ok": True}}}, required) == ["search"]
    assert inst.unhealthy_modules(
        {"modules": {"kb": {"ok": True}, "search": {"ok": True}, "x": {"ok": False}}}, required
    ) == ["x"]
    assert "kb" in inst.required_modules() and "agents" not in inst.required_modules()


def test_codex_home_that_does_not_exist_yet(tmp_home: Path, healthy: None) -> None:
    cfg = load_config(tmp_home / "c/config.toml")
    spy = Spy()
    spy.environ = {"CODEX_HOME": str(tmp_home / "new-codex-home")}
    inst.register_codex(spy, cfg, "/bin/codex")
    assert (tmp_home / "new-codex-home").is_dir()
    assert "Authorization" in (tmp_home / "new-codex-home/config.toml").read_text()
