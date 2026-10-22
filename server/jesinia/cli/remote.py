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

from jesinia.cli.install import LABEL, System, _domain, plist_path
from jesinia.core.config import Config, ConfigError, load_config, save_config, set_value

MAC_APP_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


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
        _, out = sys.run([cli, "status", "--json"])
        status = json.loads(out)
        name = str(status["Self"]["DNSName"]).strip().removesuffix(".")
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"could not read this machine's tailnet name ({exc})") from exc
    if not name:
        raise RemoteError("Tailscale reports no tailnet name; is it logged in with MagicDNS on?")
    return name


def _restart(sys: System) -> None:
    """Restart the LaunchAgent so the server adopts the new allowed host."""
    if plist_path().exists():
        sys.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"])
        sys.echo("server restarted")
    else:
        sys.echo("restart `jesinia serve` to apply")


def enable(path: Path, sys: System) -> Config:
    """Serve Jesinia on this machine's tailnet name over HTTPS and allow that host."""
    cli = tailscale_cli(sys)
    name = tailnet_name(sys, cli)
    cfg = load_config(path)
    try:
        sys.run([cli, "serve", "--bg", "--https=443", f"http://127.0.0.1:{cfg.port}"])
        cfg = set_value(path, "server.remote_host", name)
    except (RuntimeError, ConfigError) as exc:
        raise RemoteError(f"could not enable remote access: {exc}") from exc
    _restart(sys)
    sys.echo(f"remote access on: https://{name}/ (sign a device in with `jesinia login --remote`)")
    return cfg


def disable(path: Path, sys: System) -> Config:
    """Stop serving on the tailnet and forget the remote host."""
    cfg = load_config(path)
    try:
        cli = tailscale_cli(sys)
        sys.run([cli, "serve", "--https=443", "off"], check=False)
    except RemoteError as exc:
        sys.echo(f"{exc}; only clearing the config")
    cfg = replace(cfg, remote_host=None)
    save_config(cfg, path)
    _restart(sys)
    sys.echo("remote access off")
    return cfg


def describe(cfg: Config) -> str:
    if cfg.remote_host is None:
        return "remote access: off (turn it on with `jesinia remote enable`)"
    return f"remote access: https://{cfg.remote_host}/"
