# SPDX-License-Identifier: AGPL-3.0-or-later
"""``jesinia remote``: reach Jesinia from the tailnet through ``tailscale serve`` (ADR 0011).

The server keeps binding 127.0.0.1; Tailscale terminates HTTPS on this machine's tailnet
name and proxies to it. Everything goes through ``System`` so tests never touch Tailscale
or launchd.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from jesinia.cli.install import LABEL, CommandTimedOut, System, _domain, plist_path
from jesinia.core.config import Config, ConfigError, load_config, save_config, set_value

MAC_APP_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
# `tailscale serve` can wait for an approval in the browser (HTTPS certificates or Serve
# not yet enabled for the tailnet); long enough to approve, never forever.
SERVE_TIMEOUT = 60.0
STATUS_TIMEOUT = 15.0
CLI_HINT = (
    "install Tailscale's command-line tool (Tailscale menu → Settings → CLI integration) "
    "if the app's built-in one cannot start"
)


class RemoteError(RuntimeError):
    """Tailscale is missing, logged out, or refused to serve."""


def tailscale_cli(sys: System) -> str:
    """The Tailscale CLI: ``tailscale`` on PATH, else the one inside the Mac app."""
    found = sys.which("tailscale")
    if found:
        return found
    if MAC_APP_CLI.exists():
        return str(MAC_APP_CLI)
    raise RemoteError("Tailscale is not installed (https://tailscale.com/download)")


def tailnet_name(sys: System, cli: str) -> str:
    """This machine's MagicDNS name, from ``tailscale status --json``."""
    try:
        _, out = sys.run([cli, "status", "--json"], timeout=STATUS_TIMEOUT)
        status = json.loads(out)
        name = str(status["Self"]["DNSName"]).strip().removesuffix(".")
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"could not read this machine's tailnet name ({exc})") from exc
    if not name:
        raise RemoteError("Tailscale reports no tailnet name; is it logged in with MagicDNS on?")
    return name


def _funnel_is_on(sys: System, cli: str, name: str) -> bool:
    """Whether Tailscale Funnel exposes ``name:443`` to the public internet."""
    try:
        code, out = sys.run([cli, "serve", "status", "--json"], check=False, timeout=STATUS_TIMEOUT)
    except CommandTimedOut as exc:
        raise RemoteError(f"could not read `tailscale serve status` ({exc}); {CLI_HINT}") from exc
    if code != 0 or not out.strip():
        return False  # nothing served yet
    try:
        allowed = json.loads(out).get("AllowFunnel") or {}
    except (ValueError, AttributeError) as exc:
        raise RemoteError(f"could not read `tailscale serve status` ({exc})") from exc
    return bool(allowed.get(f"{name}:443"))


def _restart(sys: System) -> bool:
    """Restart the LaunchAgent so the server adopts the new allowed host; False when there
    is no LaunchAgent (a ``jesinia serve`` run by hand must be restarted by hand)."""
    if plist_path().exists():
        sys.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"])
        sys.echo("server restarted")
        return True
    sys.echo("restart `jesinia serve` to apply")
    return False


def _serve(sys: System, cli: str, port: int) -> None:
    """``tailscale serve`` with its own output on screen: when Tailscale asks for approval
    (enable HTTPS, or Serve, for the tailnet) its link shows while it waits."""
    if cli == str(MAC_APP_CLI):
        sys.echo(f"using the Tailscale app's built-in CLI; {CLI_HINT}")
    sys.echo("asking Tailscale to serve Jesinia (approve any link it prints)…")
    argv = [cli, "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"]
    try:
        code = sys.run_live(argv, timeout=SERVE_TIMEOUT)
    except CommandTimedOut as exc:
        raise RemoteError(
            f"{exc}. If Tailscale printed a link, open it, approve, and run "
            f"`jesinia remote enable` again; otherwise {CLI_HINT}"
        ) from exc
    if code != 0:
        raise RemoteError(
            f"could not enable remote access: `tailscale serve` exited with {code}; "
            "see Tailscale's output above"
        )


def enable(path: Path, sys: System) -> Config:
    """Serve Jesinia on this machine's tailnet name over HTTPS and allow that host."""
    cli = tailscale_cli(sys)
    name = tailnet_name(sys, cli)
    if _funnel_is_on(sys, cli, name):
        raise RemoteError(
            f"Tailscale Funnel is on for {name}:443, which would put Jesinia on the public "
            "internet; turn it off (`tailscale funnel --https=443 off`) and try again"
        )
    cfg = load_config(path)
    _serve(sys, cli, cfg.port)
    try:
        cfg = set_value(path, "server.remote_host", name)
    except ConfigError as exc:
        raise RemoteError(f"could not enable remote access: {exc}") from exc
    try:
        _restart(sys)
    except RuntimeError as exc:
        # Left armed, the next restart (a reboot, an update) would turn remote access on
        # after a command that reported failure: undo both halves instead.
        try:
            sys.run([cli, "serve", "--https=443", "off"], check=False, timeout=STATUS_TIMEOUT)
        except CommandTimedOut as stuck:  # best-effort: the host is forgotten regardless
            sys.echo(f"warning: {stuck}; check `tailscale serve status`")
        finally:
            save_config(replace(cfg, remote_host=None), path)
        raise RemoteError(
            f"the server did not restart ({exc}); remote access was rolled back"
        ) from exc
    sys.echo(f"remote access on: https://{name}/ (sign a device in with `jesinia login --remote`)")
    return cfg


def disable(path: Path, sys: System) -> Config:
    """Stop serving on the tailnet and forget the remote host. The host is forgotten even
    when Tailscale cannot be told to stop: once the server restarts it refuses that host,
    so a proxy left running only reaches 421s."""
    cfg = load_config(path)
    is_proxy_stopped = False
    try:
        cli = tailscale_cli(sys)
        code, _ = sys.run([cli, "serve", "--https=443", "off"], check=False, timeout=STATUS_TIMEOUT)
        is_proxy_stopped = code == 0
    except (RemoteError, CommandTimedOut) as exc:
        sys.echo(f"{exc}; only clearing the config")
    if not is_proxy_stopped:
        sys.echo(
            "warning: could not stop `tailscale serve`; check `tailscale serve status` and run "
            "`tailscale serve --https=443 off`"
        )
    cfg = replace(cfg, remote_host=None)
    save_config(cfg, path)
    if _restart(sys):
        sys.echo("remote access off: Jesinia refuses the tailnet host")
    else:
        sys.echo("remote access off once the server restarts")
    return cfg


def describe(cfg: Config) -> str:
    if cfg.remote_host is None:
        return "remote access: off (turn it on with `jesinia remote enable`)"
    return f"remote access: https://{cfg.remote_host}/"
