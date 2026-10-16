# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path, PurePosixPath

import pytest

from jesinia.core.db import open_db
from jesinia.services.kb.application.errors import PathOutsideKb
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.infrastructure.fs import LocalFiles
from jesinia.services.kb.infrastructure.index import SqliteIndex
from tests.services.kb.conftest import KB_DB

A = PageId("0192f7a8-0000-7000-8000-00000000000a")
B = PageId("0192f7a8-0000-7000-8000-00000000000b")


def test_walk_lists_files_sorted(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "z.md").write_text("z")
    (tmp_path / "a.md").write_text("a")
    assert list(LocalFiles(tmp_path).walk()) == ["a.md", "b/z.md"]


def test_contain_rejects_escapes(tmp_path: Path) -> None:
    fs = LocalFiles(tmp_path)
    for bad in ("../x", "/etc/passwd"):
        with pytest.raises(PathOutsideKb):
            fs.contain(PurePosixPath(bad))
    assert fs.contain(PurePosixPath("ok/x.md")) == (tmp_path / "ok/x.md").resolve()


def test_failed_atomic_write_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = LocalFiles(tmp_path)
    fs.write_atomic("a.md", b"v1")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        fs.write_atomic("a.md", b"v2")
    assert (tmp_path / "a.md").read_bytes() == b"v1"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


def test_rename_creates_parents(tmp_path: Path) -> None:
    fs = LocalFiles(tmp_path)
    fs.write_atomic("a.md", b"x")
    fs.rename("a.md", "deep/er/b.md")
    assert fs.exists("deep/er/b.md") and not fs.exists("a.md")


def test_index_put_get_delete_links(tmp_path: Path) -> None:
    conn = open_db(tmp_path, KB_DB)
    idx = SqliteIndex(conn)
    idx.put(A, PurePosixPath("N/a.md"), "N/a.md", "r1", "A", 1.0)
    idx.put(B, PurePosixPath("N/b.md"), "N/b.md", "r2", "B", 2.0)
    idx.set_links(B, [A])
    assert idx.links_to(A) == [B]
    assert idx.by_path(PurePosixPath("N/a.md")) == idx.get(A)
    # a new id claiming an indexed path replaces the old row
    idx.put(B, PurePosixPath("N/a.md"), "N/a.md", "r3", "B", 3.0)
    assert idx.get(A) is None and idx.get(B) is not None
    idx.delete(B)
    assert idx.get(B) is None and idx.links_to(A) == [] and idx.all() == []
    conn.close()


def test_atomic_write_ignores_planted_symlinks(tmp_path: Path) -> None:
    root, outside = tmp_path / "kb", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("keep")
    # an attacker pre-creates links at the old predictable temp name and a guessable one
    (root / ".a.md.jesinia-tmp").symlink_to(victim)
    fs = LocalFiles(root)
    fs.write_atomic("a.md", b"page")
    assert victim.read_text() == "keep" and (root / "a.md").read_bytes() == b"page"


def test_write_refuses_symlinked_parent_dir(tmp_path: Path) -> None:
    root, outside = tmp_path / "kb", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "sub").symlink_to(outside)
    with pytest.raises(PathOutsideKb):
        LocalFiles(root).write_atomic("sub/a.md", b"x")
    assert list(outside.iterdir()) == []


def test_write_refuses_symlinked_target(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "real.md").write_text("real")
    (root / "alias.md").symlink_to(root / "real.md")
    with pytest.raises(PathOutsideKb):
        LocalFiles(root).write_atomic("alias.md", b"x")
    assert (root / "real.md").read_text() == "real"


def test_walk_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "l.md").symlink_to(tmp_path / "a.md")
    (tmp_path / "d").mkdir()
    (tmp_path / "ld").symlink_to(tmp_path / "d")
    (tmp_path / "d" / "x.md").write_text("x")
    assert list(LocalFiles(tmp_path).walk()) == ["a.md", "d/x.md"]


def test_rename_refuses_symlink_source(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "real.md").write_text("real")
    (root / "link.md").symlink_to(root / "real.md")
    fs = LocalFiles(root)
    with pytest.raises(PathOutsideKb):
        fs.rename("link.md", "moved.md")
    assert (root / "real.md").read_text() == "real" and not (root / "moved.md").exists()


def test_rename_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    (root / "Real").mkdir(parents=True)
    (root / "Real/a.md").write_text("other page")
    (root / "Alias").symlink_to(root / "Real")
    fs = LocalFiles(root)
    with pytest.raises(PathOutsideKb):
        fs.rename("Alias/a.md", "moved.md")
    with pytest.raises(PathOutsideKb):
        fs.rename("x.md", "Alias/b.md")
    with pytest.raises(PathOutsideKb):
        fs.write_atomic("Alias/a.md", b"overwrite")
    assert (root / "Real/a.md").read_text() == "other page"


def test_index_persists_root(tmp_path: Path) -> None:
    conn = open_db(tmp_path, KB_DB)
    idx = SqliteIndex(conn)
    assert idx.indexed_root() is None
    idx.set_indexed_root("/a")
    idx.set_indexed_root("/b")
    assert idx.indexed_root() == "/b"
    conn.close()


def test_index_reset(tmp_path: Path) -> None:
    conn = open_db(tmp_path, KB_DB)
    idx = SqliteIndex(conn)
    idx.put(A, PurePosixPath("a.md"), "a.md", "r", "A", 1.0)
    idx.set_duplicates({A: [PurePosixPath("a.md"), PurePosixPath("b.md")]})
    idx.reset()
    assert idx.all() == [] and idx.conflicts() == []
    conn.close()
