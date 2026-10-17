# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import threading
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


def test_create_and_rename_never_clobber(tmp_path: Path) -> None:
    fs = LocalFiles(tmp_path)
    fs.write_atomic("a.md", b"a", create=True)
    with pytest.raises(FileExistsError):
        fs.write_atomic("a.md", b"other", create=True)
    fs.write_atomic("b.md", b"b", create=True)
    with pytest.raises(FileExistsError):
        fs.rename("a.md", "b.md")
    assert (tmp_path / "a.md").read_bytes() == b"a" and (tmp_path / "b.md").read_bytes() == b"b"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_case_only_rename(tmp_path: Path) -> None:
    fs = LocalFiles(tmp_path)
    fs.write_atomic("note.md", b"x", create=True)
    fs.rename("note.md", "Note.md")
    assert [p.name for p in tmp_path.iterdir()] == ["Note.md"]


def test_expected_rev_checked_before_replace(tmp_path: Path) -> None:
    import hashlib

    from jesinia.services.kb.application.errors import StaleWrite

    fs = LocalFiles(tmp_path)
    fs.write_atomic("a.md", b"v1")
    (tmp_path / "a.md").write_bytes(b"external")
    with pytest.raises(StaleWrite):
        fs.write_atomic("a.md", b"v2", expected_rev=hashlib.sha256(b"v1").hexdigest())
    assert (tmp_path / "a.md").read_bytes() == b"external" and len(list(tmp_path.iterdir())) == 1
    fs.write_atomic("a.md", b"v3", expected_rev=hashlib.sha256(b"external").hexdigest())
    assert (tmp_path / "a.md").read_bytes() == b"v3"


def test_volume_without_hard_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_links(src: object, dst: object) -> None:
        raise PermissionError("hard links not supported")

    monkeypatch.setattr(os, "link", no_links)
    fs = LocalFiles(tmp_path)
    fs.write_atomic("a.md", b"a", create=True)
    fs.rename("a.md", "b.md")
    assert (tmp_path / "b.md").read_bytes() == b"a"
    fs.write_atomic("c.md", b"c", create=True)
    with pytest.raises(FileExistsError):
        fs.rename("c.md", "b.md")


def test_read_regular_refuses_a_link_planted_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = LocalFiles(tmp_path)
    (tmp_path / "real.png").write_bytes(b"x")
    os.symlink(tmp_path / "real.png", tmp_path / "link.png")
    assert fs.read_regular("real.png") == b"x"
    # The race: the link appears between the walk and the open; O_NOFOLLOW still refuses it.
    monkeypatch.setattr(fs, "_refuse_links", lambda disk: None)
    with pytest.raises(PathOutsideKb):
        fs.read_regular("link.png")
    with pytest.raises(FileNotFoundError):
        fs.read_regular("missing/x.png")


def _read_soon(fs: LocalFiles, disk: str, seconds: float = 5.0) -> bytes | BaseException:
    """``fs.read_regular(disk)`` in a daemon thread: its result or error, failing the test if
    the read blocks (a FIFO must not hang the server)."""
    outcome: list[bytes | BaseException] = []

    def run() -> None:
        try:
            outcome.append(fs.read_regular(disk))
        except BaseException as exc:  # noqa: BLE001 - handed back to the test as the outcome
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert outcome, f"read_regular({disk!r}) blocked"
    return outcome[0]


@pytest.mark.parametrize("linked", ["_assets", f"_assets/{A}"])
def test_read_regular_never_follows_a_linked_folder_swapped_in_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    root = tmp_path / "kb"
    outside = tmp_path / "outside"
    (outside / A).mkdir(parents=True)
    (outside / A / "x.png").write_bytes(b"secret")
    (root / "_assets" / A).mkdir(parents=True)
    fs = LocalFiles(root)
    # The race: the folder becomes a link after every check, just before the open.
    monkeypatch.setattr(fs, "_refuse_links", lambda disk: None)
    monkeypatch.setattr(fs, "contain", lambda rel: root / rel)
    (root / "_assets" / A).rmdir()
    if linked == "_assets":
        (root / "_assets").rmdir()
        (root / "_assets").symlink_to(outside)
    else:
        (root / "_assets" / A).symlink_to(outside / A)
    outcome = _read_soon(fs, f"_assets/{A}/x.png")
    # Refused, never read: ELOOP (a link) on most systems, ENOTDIR on some.
    assert isinstance(outcome, PathOutsideKb | FileNotFoundError), outcome


def test_read_regular_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    (tmp_path / "_assets" / A).mkdir(parents=True)
    os.mkfifo(tmp_path / "_assets" / A / "x.png")
    outcome = _read_soon(LocalFiles(tmp_path), f"_assets/{A}/x.png")
    assert isinstance(outcome, FileNotFoundError), outcome
