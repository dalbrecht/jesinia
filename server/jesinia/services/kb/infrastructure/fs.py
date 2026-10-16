# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contained, atomic file access under the KB root."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from jesinia.services.kb.application.errors import PathOutsideKb, StaleWrite

# Atomic-write temp files; kbsync never stages them (they may vanish mid-`git add`).
TMP_SUFFIX = ".jesinia-tmp"


def _move_no_clobber(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``, failing if ``dst`` exists (atomic via link(2) + unlink)."""
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:  # a volume without hard links: check, then rename (best effort)
        if dst.exists():
            raise FileExistsError(str(dst)) from None
        os.rename(src, dst)
        return
    src.unlink()


class LocalFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def contain(self, rel: PurePosixPath) -> Path:
        if rel.is_absolute() or ".." in rel.parts:
            raise PathOutsideKb(str(rel))
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise PathOutsideKb(f"{rel} resolves outside the KB")
        return target

    def _refuse_links(self, disk: str) -> None:
        """Refuse a path if it, or any folder on the way to it, is a symlink."""
        here = self.root
        for part in PurePosixPath(disk).parts:
            here = here / part
            if os.path.islink(here):
                raise PathOutsideKb(f"{disk}: {part} is a symlink; Jesinia never writes via links")

    def _abs(self, disk: str) -> Path:
        return self.contain(PurePosixPath(disk))

    def read(self, disk: str) -> bytes:
        return self._abs(disk).read_bytes()

    def exists(self, disk: str) -> bool:
        return self._abs(disk).exists()

    def write_atomic(
        self, disk: str, data: bytes, *, expected_rev: str | None = None, create: bool = False
    ) -> float:
        """Write via temp file + fsync, then commit it at the last moment.

        ``create``: never replace an existing file (``FileExistsError``).
        ``expected_rev``: the sha256 the file must still have just before the replace;
        otherwise ``StaleWrite`` and nothing changes (an external editor saved meanwhile).
        """
        self._refuse_links(disk)
        path = self._abs(disk)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-check containment after mkdir, then create a uniquely named temp file
        # exclusively (O_EXCL: never follows or reuses a planted symlink).
        if not path.parent.resolve().is_relative_to(self.root.resolve()):
            raise PathOutsideKb(f"{disk} resolves outside the KB")
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TMP_SUFFIX)
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if create:
                _move_no_clobber(tmp, path)
            else:
                if expected_rev is not None:
                    current = path.read_bytes() if path.exists() else b""
                    if hashlib.sha256(current).hexdigest() != expected_rev:
                        raise StaleWrite(f"{disk} changed on disk during the write")
                os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return path.stat().st_mtime

    def rename(self, src: str, dst: str) -> None:
        """Move ``src`` to ``dst`` without ever replacing another file at ``dst``."""
        for name in (src, dst):
            self._refuse_links(name)
        source, target = self._abs(src), self._abs(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and os.path.samefile(source, target):
            os.rename(source, target)  # a case-only rename on a case-insensitive volume
            return
        _move_no_clobber(source, target)

    def walk(self) -> Iterator[str]:
        """Regular files below the root; symlinked files and directories are skipped."""
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(Path(dirpath) / d))
            base = Path(dirpath).relative_to(self.root)
            for name in sorted(filenames):
                if not os.path.islink(Path(dirpath) / name):
                    yield (base / name).as_posix()
