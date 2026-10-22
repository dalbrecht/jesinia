# SPDX-License-Identifier: AGPL-3.0-or-later
"""``config.toml``: load, save, and set individual keys.

The file is small and flat, so it is written by a hand-rolled serializer for the known
keys (no TOML-writing dependency). It always has mode ``0600`` because it holds the
install token.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from jesinia.core.paths import default_log_dir

# "JESI" on a phone keypad: memorable, below the ephemeral range, clear of common dev
# ports (and of macOS AirPlay on 5000/7000).
DEFAULT_PORT = 5374
DEFAULT_INTERVAL = timedelta(hours=4)
DEFAULT_QUIET = timedelta(seconds=30)
DEFAULT_FOLDERS: Mapping[str, str] = MappingProxyType(
    {"note": "Notes", "review": "Reviews", "source": "Sources"}
)
# Unkept RSS posts are pruned this long after publication (rss spec §4.4).
DEFAULT_RSS_RETENTION = timedelta(days=30)
RSS_RETENTION_DAYS_MAX = 365

_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_FOLDER_KIND = re.compile(r"^[a-z][a-z0-9_-]*$")


class ConfigError(ValueError):
    """An unknown key, an invalid value, or an unreadable config file."""


@dataclass(frozen=True)
class Config:
    token: str
    data_dir: Path
    log_dir: Path
    port: int = DEFAULT_PORT
    kb_path: Path | None = None
    kbsync_interval: timedelta = DEFAULT_INTERVAL
    kbsync_quiet: timedelta = DEFAULT_QUIET
    folders: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_FOLDERS))
    rss_retention: timedelta = DEFAULT_RSS_RETENTION


def parse_duration(text: str) -> timedelta:
    """Parse ``30d``, ``4h``, ``15m``, ``30s`` or combinations like ``1h30m``."""
    m = _DURATION.match(text.strip())
    if not text.strip() or m is None or not any(m.groups()):
        raise ConfigError(f"invalid duration {text!r} (use e.g. 30d, 4h, 15m, 30s)")
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=s)


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    d, rest = divmod(total, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    parts = [f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v]
    return "".join(parts) or "0s"


def _quote(value: str) -> str:
    # JSON string escaping is a valid subset of TOML basic strings.
    return json.dumps(value, ensure_ascii=False)


def _serialize(cfg: Config) -> str:
    lines = [
        "# Jesinia configuration. Edit with `jesinia config set <key> <value>`.",
        "[server]",
        f"port = {cfg.port}",
        f"token = {_quote(cfg.token)}",
        "",
        "[kb]",
    ]
    if cfg.kb_path is not None:
        lines.append(f"path = {_quote(str(cfg.kb_path))}")
    lines += [
        "",
        "[kbsync]",
        f"interval = {_quote(format_duration(cfg.kbsync_interval))}",
        f"quiet = {_quote(format_duration(cfg.kbsync_quiet))}",
        "",
        "[folders]",
    ]
    lines += [f"{kind} = {_quote(folder)}" for kind, folder in sorted(cfg.folders.items())]
    lines += ["", "[rss]", f"retention = {_quote(format_duration(cfg.rss_retention))}"]
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path) -> None:
    """Write ``cfg`` atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_serialize(cfg))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - only on a failed replace
            tmp.unlink()
    os.chmod(path, 0o600)


def _section(doc: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = doc.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    return value


_ALLOWED: dict[str, frozenset[str] | None] = {
    "server": frozenset({"port", "token"}),
    "kb": frozenset({"path"}),
    "kbsync": frozenset({"interval", "quiet"}),
    "folders": None,  # any folder kind; validated by _apply
    "rss": frozenset({"retention"}),
}


_TYPES: dict[tuple[str, str], type] = {
    ("server", "port"): int,
    ("server", "token"): str,
    ("kb", "path"): str,
    ("kbsync", "interval"): str,
    ("kbsync", "quiet"): str,
    ("rss", "retention"): str,
}


def _check_types(doc: Mapping[str, object]) -> None:
    for table in _ALLOWED:
        for key, value in _section(doc, table).items():
            expected = _TYPES.get((table, key), str)  # every [folders] value is a string
            if isinstance(value, bool) or not isinstance(value, expected):
                raise ConfigError(f"[{table}] {key} must be a {expected.__name__}, got {value!r}")


def _check_keys(doc: Mapping[str, object]) -> None:
    for table in doc:
        if table not in _ALLOWED:
            raise ConfigError(f"unknown section [{table}]")
    for table, allowed in _ALLOWED.items():
        section = _section(doc, table)
        if allowed is None:
            continue
        unknown = sorted(set(section) - allowed)
        if unknown:
            raise ConfigError(f"unknown key(s) in [{table}]: {', '.join(unknown)}")


def _from_doc(doc: Mapping[str, object], path: Path) -> Config:
    _check_keys(doc)
    _check_types(doc)
    server, kb, kbsync, folders, rss = (
        _section(doc, s) for s in ("server", "kb", "kbsync", "folders", "rss")
    )
    token = server.get("token")
    if not isinstance(token, str) or not token:
        raise ConfigError("[server] token is missing")
    cfg = Config(token=token, data_dir=path.parent, log_dir=default_log_dir())
    if "port" in server:
        cfg = _apply(cfg, "server.port", str(server["port"]))
    if "path" in kb:
        cfg = _apply(cfg, "kb.path", str(kb["path"]))
    for key in ("interval", "quiet"):
        if key in kbsync:
            cfg = _apply(cfg, f"kbsync.{key}", str(kbsync[key]))
    if folders:
        cfg = replace(cfg, folders={})
        for kind, folder in folders.items():
            cfg = _apply(cfg, f"folders.{kind}", str(folder))
    if "retention" in rss:
        cfg = _apply(cfg, "rss.retention", str(rss["retention"]))
    return cfg


def load_config(path: Path) -> Config:
    """Read ``path``; create it with defaults and a fresh token if it does not exist."""
    if not path.exists():
        cfg = Config(
            token=secrets.token_urlsafe(32), data_dir=path.parent, log_dir=default_log_dir()
        )
        save_config(cfg, path)
        return cfg
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return _from_doc(doc, path)


def _validate_folder(raw: str) -> str:
    folder = raw.strip().strip("/") if raw.strip() != "/" else ""
    pure = PurePosixPath(raw.strip())
    if not folder or pure.is_absolute() or ".." in pure.parts:
        raise ConfigError(f"invalid folder {raw!r}: use a relative path inside the KB")
    return folder


def _apply(cfg: Config, key: str, raw: str) -> Config:
    if key == "server.port":
        try:
            port = int(raw)
        except ValueError as exc:
            raise ConfigError(f"server.port must be an integer, got {raw!r}") from exc
        if not 1 <= port <= 65535:
            raise ConfigError(f"server.port out of range: {port}")
        return replace(cfg, port=port)
    if key == "kb.path":
        if not raw.strip():
            return replace(cfg, kb_path=None)
        kb = Path(raw).expanduser()
        if not kb.is_absolute():
            raise ConfigError(f"kb.path must be absolute, got {raw!r}")
        return replace(cfg, kb_path=kb)
    if key in ("kbsync.interval", "kbsync.quiet"):
        td = parse_duration(raw)
        if td <= timedelta(0):
            raise ConfigError(f"{key} must be positive")
        if key == "kbsync.interval":
            return replace(cfg, kbsync_interval=td)
        return replace(cfg, kbsync_quiet=td)
    if key == "rss.retention":
        td = parse_duration(raw)
        whole_days = td.days if td == timedelta(days=td.days) else 0
        if not 1 <= whole_days <= RSS_RETENTION_DAYS_MAX:
            raise ConfigError(
                f"rss.retention must be 1 to {RSS_RETENTION_DAYS_MAX} whole days (e.g. 30d), "
                f"got {raw!r}"
            )
        return replace(cfg, rss_retention=td)
    if key.startswith("folders."):
        kind = key.removeprefix("folders.")
        if not _FOLDER_KIND.match(kind):
            raise ConfigError(f"invalid folder kind {kind!r}")
        return replace(cfg, folders={**cfg.folders, kind: _validate_folder(raw)})
    raise ConfigError(
        f"unknown key {key!r} (known: server.port, kb.path, kbsync.interval, "
        "kbsync.quiet, folders.<kind>, rss.retention)"
    )


def set_value(path: Path, key: str, raw: str) -> Config:
    """Set one key, persist, and return the new config."""
    return set_values(path, {key: raw})


def set_values(path: Path, values: Mapping[str, str]) -> Config:
    """Set several keys in one write (all validated first), persist, return the new config."""
    cfg = load_config(path)
    for key, raw in values.items():
        cfg = _apply(cfg, key, raw)
    save_config(cfg, path)
    return cfg
