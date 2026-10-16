# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``jesinia`` command."""

from __future__ import annotations

import json
import logging
import logging.handlers
import secrets
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from jesinia.cli import backup as backup_mod
from jesinia.cli import doctor as doctor_mod
from jesinia.cli.server import ServerDown, call
from jesinia.core.config import Config, ConfigError, format_duration, load_config, save_config
from jesinia.core.config import set_value as config_set_value
from jesinia.core.db import DbClass
from jesinia.core.paths import config_path
from jesinia.registry import DATABASES, modules

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Jesinia server and tools.")
config_app = typer.Typer(no_args_is_help=True, help="Read or change config.toml.")
token_app = typer.Typer(no_args_is_help=True, help="The install token (MCP + CLI auth).")
sync_app = typer.Typer(no_args_is_help=True, help="KB git sync.")
app.add_typer(config_app, name="config")
app.add_typer(token_app, name="token")
app.add_typer(sync_app, name="sync")

LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5


def _cfg() -> Config:
    try:
        return load_config(config_path())
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(log_dir: Path) -> logging.Handler:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "server.log", maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler


@app.command()
def serve() -> None:
    """Run the server on 127.0.0.1 (launchd runs this)."""
    import uvicorn

    from jesinia.core.app import create_app

    cfg = _cfg()
    configure_logging(cfg.log_dir)
    uvicorn.run(
        create_app(cfg, modules()),
        host="127.0.0.1",
        port=cfg.port,
        log_config=None,
        proxy_headers=False,
    )


@app.command()
def status() -> None:
    """Server health, and a fresh one-time login link for the browser."""
    cfg = _cfg()
    try:
        health = call(cfg, "GET", "/healthz")
        login = call(cfg, "POST", "/api/auth/nonce")
    except ServerDown as exc:
        raise _fail(f"{exc} (start it with ./setup or `jesinia serve`)") from exc
    typer.echo(f"server: {'ok' if health['ok'] else 'degraded'} on port {cfg.port}")
    for name, m in health["modules"].items():
        typer.echo(f"  {'ok  ' if m['ok'] else 'FAIL'} {name}: {m['detail']}")
    typer.echo(f"login: {login['url']}")


@app.command()
def doctor() -> None:
    """Check the install; exit 1 if anything fails."""
    checks = doctor_mod.doctor(config_path())
    for check in checks:
        typer.echo(check.line())
    if any(c.level == "fail" for c in checks):
        raise typer.Exit(1)


_GETTERS = {
    "server.port": lambda c: str(c.port),
    "kb.path": lambda c: str(c.kb_path) if c.kb_path else "",
    "kbsync.interval": lambda c: format_duration(c.kbsync_interval),
    "kbsync.quiet": lambda c: format_duration(c.kbsync_quiet),
}


@config_app.command("get")
def config_get(key: str) -> None:
    """Print one value (``server.port``, ``kb.path``, ``kbsync.*``, ``folders.<kind>``)."""
    cfg = _cfg()
    if key in _GETTERS:
        typer.echo(_GETTERS[key](cfg))
    elif key.startswith("folders.") and key.removeprefix("folders.") in cfg.folders:
        typer.echo(cfg.folders[key.removeprefix("folders.")])
    else:
        raise _fail(f"unknown key {key!r} (the token is shown by `jesinia token show`)")


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Change one value. The server picks most changes up on restart."""
    try:
        config_set_value(config_path(), key, value)
    except ConfigError as exc:
        raise _fail(str(exc), 2) from exc
    typer.echo(f"{key} = {value}")


@token_app.command("show")
def token_show() -> None:
    """Print the install token."""
    typer.echo(_cfg().token)


def reregister_agents(cfg: Config) -> None:
    """Refresh agent MCP registrations that embed the token (filled in by the installer)."""


@token_app.command("rotate")
def token_rotate() -> None:
    """Issue a new token, sign out browsers, and update agent registrations."""
    old = _cfg()
    new = replace(old, token=secrets.token_urlsafe(32))
    save_config(new, config_path())
    try:
        call(old, "POST", "/api/auth/reload-token", token=old.token)
        typer.echo("server: now using the new token; browser sessions signed out")
    except ServerDown:
        typer.echo("server: not running; it will use the new token when it starts")
    reregister_agents(new)
    typer.echo("token rotated")


@sync_app.command("now")
def sync_now() -> None:
    """Run a KB sync cycle now."""
    try:
        r = call(_cfg(), "POST", "/api/kbsync/run", timeout=300)
    except ServerDown as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"{r['result']['outcome']}: {r['result']['message']}")


@app.command()
def reindex() -> None:
    """Rebuild derived data: rescan the KB and the search index (or, with the server
    stopped, delete derived databases so the next start rebuilds them)."""
    cfg = _cfg()
    try:
        scan = call(cfg, "POST", "/api/kb/scan", timeout=600)
        search = call(cfg, "POST", "/api/search/reindex", timeout=600)
    except ServerDown:
        for spec in DATABASES:
            if spec.db_class is DbClass.DERIVED:
                for suffix in ("", "-wal", "-shm"):
                    (cfg.data_dir / f"{spec.name}.sqlite{suffix}").unlink(missing_ok=True)
        typer.echo("server not running: derived databases removed; they rebuild on start")
        return
    typer.echo(f"kb: {scan['indexed']} pages; search: {search['indexed']} pages")


@app.command()
def backup(
    dest: Annotated[Path | None, typer.Option(help="Folder for the backup")] = None,
) -> None:
    """Copy history databases and config.toml to a timestamped folder."""
    cfg = _cfg()
    out = backup_mod.backup(cfg.data_dir, DATABASES, dest)
    typer.echo(str(out))


@app.command()
def openapi() -> None:
    """Print the server's OpenAPI document (the web client is generated from it)."""
    from jesinia.core.app import create_app

    cfg = _cfg()
    sys.stdout.write(json.dumps(create_app(cfg, modules()).openapi(), indent=2) + "\n")
