# SPDX-License-Identifier: AGPL-3.0-or-later
"""One SQLite database per module, with numbered SQL migrations (spec §3.4)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources.abc import Traversable
from pathlib import Path

_MIGRATION = re.compile(r"^(\d{4})_[\w.-]+\.sql$")


class DbClass(StrEnum):
    """How a database may be treated if lost (spec §3.4 rebuild rule)."""

    DERIVED = "derived"  # rebuildable from KB files / remote APIs (`jesinia reindex`)
    CACHE = "cache"  # disposable
    HISTORY = "history"  # not reconstructible; included in `jesinia backup`


@dataclass(frozen=True)
class ModuleDb:
    name: str
    db_class: DbClass
    migrations: Traversable


class MigrationError(RuntimeError):
    """A migration failed (and was rolled back) or the migration set is invalid."""


def _migrations(source: Traversable) -> list[tuple[int, str, str]]:
    found: dict[int, tuple[str, str]] = {}
    for entry in source.iterdir():
        m = _MIGRATION.match(entry.name)
        if not m:
            continue
        version = int(m.group(1))
        if version == 0:
            raise MigrationError(f"{entry.name}: migration numbers start at 0001")
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: {found[version][0]}, {entry.name}"
            )
        found[version] = (entry.name, entry.read_text(encoding="utf-8"))
    return [(v, *found[v]) for v in sorted(found)]


def _statements(sql: str) -> list[str]:
    """Split at SQLite statement boundaries (``;`` outside strings/comments), not lines."""
    out: list[str] = []
    start = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start : i + 1]):
            out.append(sql[start : i + 1])
            start = i + 1
    rest = sql[start:]
    if any(ln.strip() and not ln.strip().startswith("--") for ln in rest.splitlines()):
        out.append(rest)
    return out


def open_db(data_dir: Path, spec: ModuleDb) -> sqlite3.Connection:
    """Open ``<data_dir>/<name>.sqlite`` (WAL, foreign keys on) and apply pending migrations.

    Each migration runs in its own transaction; on failure it is rolled back and
    ``MigrationError`` names the file.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / f"{spec.name}.sqlite", isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    try:
        pending = [m for m in _migrations(spec.migrations) if m[0] > current]
    except MigrationError:
        conn.close()
        raise
    for version, name, sql in pending:
        try:
            conn.execute("BEGIN")
            for stmt in _statements(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            conn.close()
            raise MigrationError(f"{spec.name}: migration {name} failed: {exc}") from exc
    return conn
