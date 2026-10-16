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

HEALTHY = {"ok": True, "modules": {"kb": {"ok": True, "detail": "ok"}}}


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
    assert (
        p["Label"] == "com.jesinia.server"
        and p["KeepAlive"] is True
        and p["ThrottleInterval"] == 10
    )
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
    assert spy.out[-1].endswith("Open: http://127.0.0.1:5374/login?nonce=n")

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
        respx.get("http://127.0.0.1:5374/healthz").mock(side_effect=ConnectError("x"))
        assert inst._wait_healthy(Spy(), cfg) is False
        respx.get("http://127.0.0.1:5374/healthz").mock(return_value=Response(200, json=HEALTHY))
        assert inst._wait_healthy(Spy(), cfg) is True
    monkeypatch.setattr(inst, "HEALTH_TIMEOUT", 5.0)
    slept: list[float] = []
    spy = Spy()
    spy.sleep = slept.append
    with respx.mock:
        respx.get("http://127.0.0.1:5374/healthz").mock(
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
        "modules": {"kb": {"ok": True, "detail": "ok"}, "agents": {"ok": False, "detail": "x"}},
    }
    with respx.mock:
        respx.get("http://127.0.0.1:5374/healthz").mock(return_value=Response(200, json=degraded))
        assert inst._wait_healthy(Spy(), cfg) is False
        respx.get("http://127.0.0.1:5374/healthz").mock(
            return_value=Response(200, json=optional_only)
        )
        assert inst._wait_healthy(Spy(), cfg) is True
        respx.get("http://127.0.0.1:5374/healthz").mock(return_value=Response(200, text="not json"))
        assert inst._wait_healthy(Spy(), cfg) is False
    assert inst.unhealthy_modules([]) == ["?"]


def test_updater_plist(tmp_path: Path) -> None:
    p = plistlib.loads(
        inst.render_updater_plist(Path("/opt/uv"), Path("/r"), tmp_path, "/usr/bin").encode()
    )
    assert (
        p["Label"] == "com.jesinia.updater" and p["StartInterval"] == 900 and p["RunAtLoad"] is True
    )
    assert p["ProgramArguments"][-2:] == ["jesinia", "update"]
    assert p["StandardOutPath"] == str(tmp_path / "update.log")


def test_auto_update_on_then_off(tmp_home: Path, kb: Path, healthy: None) -> None:
    updater = tmp_home / "Library/LaunchAgents/com.jesinia.updater.plist"
    spy = Spy(yes=True)
    assert inst.install(kb, spy, auto_update=True) == 0
    assert updater.exists()
    assert ["launchctl", "bootstrap"] in [c[:2] for c in spy.calls if "updater" in " ".join(c)]
    assert inst.install(kb, Spy(yes=True)) == 0 and updater.exists()  # None keeps it
    assert inst.install(kb, Spy(yes=True), auto_update=False) == 0 and not updater.exists()
    inst.install(kb, Spy(yes=True), auto_update=True)
    inst.uninstall(Spy(yes=True), purge_data=False)
    assert not updater.exists()


def test_agents_get_the_plugin_and_codex_instructions(
    tmp_home: Path, kb: Path, healthy: None
) -> None:
    agents_md = tmp_home / ".codex/AGENTS.md"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text("# My own rules\n\nBe brief.\n")
    spy = Spy(yes=True)
    inst.install(kb, spy)
    assert ["/bin/claude", "plugin", "install", "jesinia@jesinia", "--scope", "user"] in spy.calls
    assert any(
        c[:4] == ["/bin/claude", "plugin", "marketplace", "add"]
        and c[4].endswith("skills/claude-code")
        for c in spy.calls
    )
    text = agents_md.read_text()
    assert text.startswith("# My own rules") and text.count("<!-- jesinia:begin") == 1
    inst.install(kb, Spy(yes=True))  # re-install: still exactly one block
    assert agents_md.read_text().count("<!-- jesinia:begin") == 1
    spy2 = Spy(yes=True)
    inst.uninstall(spy2, purge_data=False)
    assert agents_md.read_text() == "# My own rules\n\nBe brief.\n"
    assert ["/bin/claude", "plugin", "uninstall", "jesinia@jesinia"] in spy2.calls
    inst.uninstall(Spy(yes=True), purge_data=False)  # no block: unchanged
    agents_md.unlink()
    inst.uninstall(Spy(yes=True), purge_data=False)  # no file: nothing created
    assert not agents_md.exists()
    dry = Spy(dry_run=True)
    inst.set_codex_instructions(dry, True)
    assert "would update the Jesinia section" in dry.out[-1]


def test_command_shim(tmp_home: Path, kb: Path, healthy: None) -> None:
    shim = tmp_home / ".local/bin/jesinia"
    spy = Spy(yes=True)
    inst.install(kb, spy)
    text = shim.read_text()
    assert (
        text.startswith("#!/bin/sh\n# jesinia command shim")
        and '"/bin/uv" run --quiet --project' in text
    )
    assert shim.stat().st_mode & 0o777 == 0o755
    assert any("add" in o and ".local/bin" in o for o in spy.out)  # not on the spy's PATH
    inst.uninstall(Spy(yes=True), purge_data=False)
    assert not shim.exists()
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\necho mine\n")
    other = Spy(yes=True)
    inst.install(kb, other)
    assert shim.read_text() == "#!/bin/sh\necho mine\n" and any(
        "left alone" in o for o in other.out
    )
    inst.uninstall(Spy(yes=True), purge_data=False)
    assert shim.exists()  # someone else's file is never removed
    on_path = Spy(yes=True)
    on_path.environ = {**on_path.environ, "PATH": f"{shim.parent}:/usr/bin"}
    shim.unlink()
    inst.install(kb, on_path)
    assert not any("add" in o and ".local/bin" in o for o in on_path.out)
