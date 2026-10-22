# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jesinia.cli import main, remote
from jesinia.cli.install import CommandTimedOut, System
from jesinia.core.config import load_config, set_value

NAME = "mac.tail1234.ts.net"
BASE = "http://127.0.0.1:5374"


@dataclass
class FakeTailscale(System):
    """Records commands; answers ``tailscale status --json`` with ``status``."""

    calls: list[list[str]] = field(default_factory=list)
    out: list[str] = field(default_factory=list)
    status: object = field(default_factory=lambda: {"Self": {"DNSName": f"{NAME}."}})
    on_path: bool = True
    failing: str = ""
    serve_status: str = ""
    off_code: int = 0
    # `tailscale serve --bg` waiting on a prompt nobody answers (approve HTTPS, a GUI that
    # never starts) until the timeout.
    serve_hangs: bool = False
    live: list[tuple[list[str], float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.echo = self.out.append
        self.which = lambda n: f"/usr/local/bin/{n}" if self.on_path else None

    def run(
        self, argv: list[str], *, check: bool = True, timeout: float | None = None
    ) -> tuple[int, str]:
        self.calls.append(argv)
        assert timeout is not None or argv[0] == "launchctl", f"no timeout on {argv}"
        if self.failing and self.failing in argv and check:
            raise RuntimeError(f"{' '.join(argv)} failed: nope")
        if argv[1:] == ["status", "--json"]:
            return 0, self.status if isinstance(self.status, str) else json.dumps(self.status)
        if argv[1:] == ["serve", "status", "--json"]:
            return 0, self.serve_status
        if argv[-1] == "off":
            return self.off_code, ""
        return 0, ""

    def run_live(self, argv: list[str], *, timeout: float) -> int:
        self.calls.append(argv)
        self.live.append((argv, timeout))
        if self.serve_hangs:
            raise CommandTimedOut(f"{' '.join(argv)} did not finish in {timeout:.0f}s")
        return 1 if self.failing and self.failing in argv else 0


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(remote, "plist_path", lambda: tmp_path / "missing.plist")
    path = tmp_path / "config.toml"
    load_config(path)
    return path


def test_enable_should_serve_the_tailnet_name_and_allow_it(config: Path) -> None:
    sys = FakeTailscale()
    cfg = remote.enable(config, sys)
    assert cfg.remote_host == NAME and load_config(config).remote_host == NAME
    assert [
        "/usr/local/bin/tailscale",
        "serve",
        "--bg",
        "--https=443",
        "http://127.0.0.1:5374",
    ] in sys.calls
    assert any("restart `jesinia serve`" in line for line in sys.out)
    assert any(f"https://{NAME}/" in line for line in sys.out)


def test_enable_should_restart_the_launch_agent_when_installed(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_text("x")
    monkeypatch.setattr(remote, "plist_path", lambda: plist)
    sys = FakeTailscale()
    remote.enable(config, sys)
    assert sys.calls[-1][:3] == ["launchctl", "kickstart", "-k"]
    assert sys.calls[-1][3].endswith("/com.jesinia.server")


def test_should_use_the_mac_app_cli_when_tailscale_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_cli = tmp_path / "Tailscale"
    app_cli.write_text("")
    monkeypatch.setattr(remote, "MAC_APP_CLI", app_cli)
    assert remote.tailscale_cli(FakeTailscale(on_path=False)) == str(app_cli)


def test_should_explain_when_tailscale_is_missing(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote, "MAC_APP_CLI", tmp_path / "nope")
    with pytest.raises(remote.RemoteError, match="not installed"):
        remote.enable(config, FakeTailscale(on_path=False))
    assert load_config(config).remote_host is None


@pytest.mark.parametrize(
    "status", ["not json", {"Self": {}}, {"Self": {"DNSName": ""}}, {"Self": None}]
)
def test_should_explain_when_the_tailnet_name_is_unknown(config: Path, status: object) -> None:
    with pytest.raises(remote.RemoteError, match="tailnet name"):
        remote.enable(config, FakeTailscale(status=status))
    assert load_config(config).remote_host is None


def test_should_keep_remote_access_off_when_serve_fails(config: Path) -> None:
    sys = FakeTailscale(failing="serve")
    with pytest.raises(remote.RemoteError, match="could not enable remote access"):
        remote.enable(config, sys)
    assert load_config(config).remote_host is None


def test_disable_should_stop_serving_and_clear_the_host(config: Path) -> None:
    set_value(config, "server.remote_host", NAME)
    sys = FakeTailscale()
    assert remote.disable(config, sys).remote_host is None
    assert load_config(config).remote_host is None
    assert ["/usr/local/bin/tailscale", "serve", "--https=443", "off"] in sys.calls
    assert "remote access off once the server restarts" in sys.out
    assert not any("warning" in line for line in sys.out)


def test_disable_should_warn_when_tailscale_keeps_serving(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_text("x")
    monkeypatch.setattr(remote, "plist_path", lambda: plist)
    set_value(config, "server.remote_host", NAME)
    sys = FakeTailscale(off_code=1)
    remote.disable(config, sys)
    assert load_config(config).remote_host is None
    assert any("could not stop `tailscale serve`" in line for line in sys.out)
    assert "remote access off: Jesinia refuses the tailnet host" in sys.out


def test_enable_should_refuse_when_funnel_is_on(config: Path) -> None:
    status = json.dumps({"AllowFunnel": {f"{NAME}:443": True}})
    sys = FakeTailscale(serve_status=status)
    with pytest.raises(remote.RemoteError, match="Funnel is on"):
        remote.enable(config, sys)
    assert load_config(config).remote_host is None
    assert not any("--bg" in argv for argv in sys.calls)


def test_enable_should_proceed_when_funnel_is_off_elsewhere(config: Path) -> None:
    status = json.dumps({"AllowFunnel": {"other.ts.net:443": True}, "TCP": {}})
    assert remote.enable(config, FakeTailscale(serve_status=status)).remote_host == NAME
    assert remote.enable(config, FakeTailscale(serve_status="{}")).remote_host == NAME


def test_enable_should_explain_an_unreadable_serve_status(config: Path) -> None:
    with pytest.raises(remote.RemoteError, match="serve status"):
        remote.enable(config, FakeTailscale(serve_status="[1]"))


def test_disable_should_clear_the_host_even_without_tailscale(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote, "MAC_APP_CLI", tmp_path / "nope")
    set_value(config, "server.remote_host", NAME)
    sys = FakeTailscale(on_path=False)
    remote.disable(config, sys)
    assert load_config(config).remote_host is None
    assert any("only clearing the config" in line for line in sys.out)


# -- the commands -----------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("JESINIA_HOME", str(h))
    monkeypatch.setattr(remote, "plist_path", lambda: tmp_path / "missing.plist")
    return h


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(main.app, args)


def test_remote_commands_should_enable_report_and_disable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTailscale()
    monkeypatch.setattr(main, "_remote_system", lambda: fake)
    assert "off" in _invoke(["remote", "status"]).stdout
    assert _invoke(["remote", "enable"]).exit_code == 0
    assert f"https://{NAME}/" in _invoke(["remote", "status"]).stdout
    assert _invoke(["config", "get", "server.remote_host"]).stdout.strip() == NAME
    assert _invoke(["remote", "disable"]).exit_code == 0
    assert "off" in _invoke(["remote", "status"]).stdout


def test_remote_enable_should_fail_with_the_reason(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote, "MAC_APP_CLI", tmp_path / "nope")
    monkeypatch.setattr(main, "_remote_system", lambda: FakeTailscale(on_path=False))
    r = _invoke(["remote", "enable"])
    assert r.exit_code == 1 and "not installed" in r.stderr


def test_remote_disable_should_fail_with_the_reason(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "_remote_system", lambda: FakeTailscale(failing="launchctl"))

    def broken(path: Path, sys: System) -> None:
        raise RuntimeError("launchctl kickstart failed")

    monkeypatch.setattr(remote, "disable", broken)
    r = _invoke(["remote", "disable"])
    assert r.exit_code == 1 and "kickstart failed" in r.stderr


def test_the_real_remote_system_should_echo_through_typer() -> None:
    assert isinstance(main._remote_system(), System)


@respx.mock
def test_login_remote_should_print_the_tailnet_link(home: Path) -> None:
    _invoke(["config", "set", "server.remote_host", NAME])
    respx.post(f"{BASE}/api/auth/nonce").mock(
        return_value=httpx.Response(
            200,
            json={
                "nonce": "n",
                "url": f"{BASE}/login?nonce=n",
                "remote_url": f"https://{NAME}/login?nonce=n",
            },
        )
    )
    r = _invoke(["login", "--remote"])
    assert r.exit_code == 0 and f"https://{NAME}/login?nonce=n" in r.stdout


def test_login_remote_should_refuse_when_remote_access_is_off(home: Path) -> None:
    r = _invoke(["login", "--remote"])
    assert r.exit_code == 1 and "remote access is off" in r.stderr


@respx.mock
def test_login_remote_should_ask_for_a_restart_when_the_server_lags(home: Path) -> None:
    _invoke(["config", "set", "server.remote_host", NAME])
    respx.post(f"{BASE}/api/auth/nonce").mock(
        return_value=httpx.Response(
            200, json={"nonce": "n", "url": f"{BASE}/login?nonce=n", "remote_url": None}
        )
    )
    r = _invoke(["login", "--remote"])
    assert r.exit_code == 1 and "restart" in r.stderr


@respx.mock
def test_status_should_print_the_remote_login_link(home: Path) -> None:
    respx.get(f"{BASE}/healthz").mock(
        return_value=httpx.Response(200, json={"ok": True, "modules": {}})
    )
    respx.post(f"{BASE}/api/auth/nonce").mock(
        return_value=httpx.Response(
            200,
            json={
                "nonce": "n",
                "url": f"{BASE}/login?nonce=n",
                "remote_url": f"https://{NAME}/login?nonce=n",
            },
        )
    )
    r = _invoke(["status"])
    assert f"login from another device: https://{NAME}/login?nonce=n" in r.stdout


def test_enable_should_roll_back_when_the_server_cannot_restart(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_text("x")
    monkeypatch.setattr(remote, "plist_path", lambda: plist)
    sys = FakeTailscale(failing="kickstart")
    with pytest.raises(remote.RemoteError, match="rolled back"):
        remote.enable(config, sys)
    assert load_config(config).remote_host is None
    assert ["/usr/local/bin/tailscale", "serve", "--https=443", "off"] in sys.calls


def test_enable_should_show_tailscale_serve_output_and_bound_its_wait(config: Path) -> None:
    sys = FakeTailscale()
    remote.enable(config, sys)
    [(argv, timeout)] = sys.live
    assert argv[1:3] == ["serve", "--bg"] and timeout == remote.SERVE_TIMEOUT


def test_enable_should_explain_a_serve_that_never_finishes(config: Path) -> None:
    # Regression: `tailscale serve` waited for an approval nobody could see, and `remote
    # enable` hung with no output.
    with pytest.raises(remote.RemoteError) as raised:
        remote.enable(config, FakeTailscale(serve_hangs=True))
    message = str(raised.value)
    assert "did not finish" in message and "approve" in message
    assert "CLI integration" in message
    assert load_config(config).remote_host is None


def test_enable_should_report_a_failed_serve_with_what_tailscale_printed(config: Path) -> None:
    with pytest.raises(remote.RemoteError, match="exited with 1; see Tailscale's output above"):
        remote.enable(config, FakeTailscale(failing="--bg"))


def test_enable_should_point_to_the_cli_when_using_the_app_binary(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_cli = tmp_path / "Tailscale"
    app_cli.write_text("")
    monkeypatch.setattr(remote, "MAC_APP_CLI", app_cli)
    sys = FakeTailscale(on_path=False)
    remote.enable(config, sys)
    assert any("CLI integration" in line for line in sys.out)


def test_status_calls_should_time_out_with_the_reason(config: Path) -> None:
    class Stuck(FakeTailscale):
        def run(
            self, argv: list[str], *, check: bool = True, timeout: float | None = None
        ) -> tuple[int, str]:
            raise CommandTimedOut(f"{argv[0]} did not finish in {timeout:.0f}s")

    with pytest.raises(remote.RemoteError, match="did not finish"):
        remote.enable(config, Stuck())


def test_disable_should_clear_the_host_when_tailscale_does_not_answer(config: Path) -> None:
    class Stuck(FakeTailscale):
        def run(
            self, argv: list[str], *, check: bool = True, timeout: float | None = None
        ) -> tuple[int, str]:
            if argv[-1] == "off":
                raise CommandTimedOut("tailscale serve did not finish in 15s")
            return super().run(argv, check=check, timeout=timeout)

    set_value(config, "server.remote_host", NAME)
    sys = Stuck()
    remote.disable(config, sys)
    assert load_config(config).remote_host is None
    assert any("did not finish" in line for line in sys.out)


def test_enable_rollback_should_clear_the_host_even_when_tailscale_does_not_answer(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stuck(FakeTailscale):
        def run(
            self, argv: list[str], *, check: bool = True, timeout: float | None = None
        ) -> tuple[int, str]:
            if argv[-1] == "off":
                raise CommandTimedOut("tailscale serve did not finish in 15s")
            return super().run(argv, check=check, timeout=timeout)

    plist = tmp_path / "agent.plist"
    plist.write_text("x")
    monkeypatch.setattr(remote, "plist_path", lambda: plist)
    sys = Stuck(failing="kickstart")
    with pytest.raises(remote.RemoteError, match="rolled back"):
        remote.enable(config, sys)
    assert load_config(config).remote_host is None
    assert any("did not finish" in line for line in sys.out)
