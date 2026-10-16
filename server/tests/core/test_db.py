# SPDX-License-Identifier: AGPL-3.0-or-later
import sqlite3
from pathlib import Path

import pytest

from jesinia.core.db import DbClass, MigrationError, ModuleDb, open_db


@pytest.fixture
def fake_migrations(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0002_add_col.sql").write_text("ALTER TABLE items ADD COLUMN note TEXT;\n")
    (d / "0001_init.sql").write_text(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
        "-- a comment; with a semicolon\n"
        "INSERT INTO items (name) VALUES ('a;b');\n"
    )
    (d / "README.md").write_text("not a migration\n")
    return d


@pytest.fixture
def broken_migrations(tmp_path: Path) -> Path:
    d = tmp_path / "broken"
    d.mkdir()
    (d / "0001_init.sql").write_text("CREATE TABLE items (id INTEGER PRIMARY KEY);\n")
    (d / "0002_bad.sql").write_text(
        "CREATE TABLE half (id INTEGER);\nINSERT INTO nowhere VALUES (1);\n"
    )
    return d


def test_migrations_apply_once_in_order(tmp_path: Path, fake_migrations: Path) -> None:
    conn = open_db(tmp_path, ModuleDb("t", DbClass.DERIVED, fake_migrations))
    assert conn.execute("select max(version) from schema_version").fetchone()[0] == 2
    assert conn.execute("select name, note from items").fetchall() == [("a;b", None)]
    conn.close()
    conn = open_db(tmp_path, ModuleDb("t", DbClass.DERIVED, fake_migrations))  # idempotent
    assert conn.execute("select count(*) from items").fetchone()[0] == 1
    assert conn.execute("select count(*) from schema_version").fetchone()[0] == 2
    conn.close()


def test_wal_and_foreign_keys(tmp_path: Path, fake_migrations: Path) -> None:
    conn = open_db(tmp_path, ModuleDb("t", DbClass.CACHE, fake_migrations))
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
    conn.close()
    assert (tmp_path / "t.sqlite").exists()


def test_failed_migration_raises_and_rolls_back(tmp_path: Path, broken_migrations: Path) -> None:
    with pytest.raises(MigrationError, match="0002_bad.sql"):
        open_db(tmp_path, ModuleDb("t", DbClass.DERIVED, broken_migrations))
    conn = sqlite3.connect(tmp_path / "t.sqlite")
    assert conn.execute("select max(version) from schema_version").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert "half" not in tables and "items" in tables
    conn.close()


def test_duplicate_version_numbers_rejected(tmp_path: Path) -> None:
    d = tmp_path / "dup"
    d.mkdir()
    (d / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER);\n")
    (d / "0001_b.sql").write_text("CREATE TABLE b (id INTEGER);\n")
    with pytest.raises(MigrationError, match="duplicate"):
        open_db(tmp_path, ModuleDb("t", DbClass.HISTORY, d))


def test_db_class_values() -> None:
    assert [c.value for c in DbClass] == ["derived", "cache", "history"]


def test_creates_data_dir(tmp_path: Path, fake_migrations: Path) -> None:
    target = tmp_path / "nested" / "data"
    open_db(target, ModuleDb("t", DbClass.DERIVED, fake_migrations)).close()
    assert (target / "t.sqlite").exists()
