# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contained, atomic file access under the KB root."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from jesinia.services.kb.application.errors import PathOutsideKb


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

    def _abs(self, disk: str) -> Path:
        return self.contain(PurePosixPath(disk))

    def read(self, disk: str) -> bytes:
        return self._abs(disk).read_bytes()

    def exists(self, disk: str) -> bool:
        return self._abs(disk).exists()

    def write_atomic(self, disk: str, data: bytes) -> float:
        if os.path.islink(self.root / disk):
            raise PathOutsideKb(f"{disk} is a symlink; Jesinia never writes through links")
        path = self._abs(disk)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-check containment after mkdir, then create a uniquely named temp file
        # exclusively (O_EXCL: never follows or reuses a planted symlink).
        if not path.parent.resolve().is_relative_to(self.root.resolve()):
            raise PathOutsideKb(f"{disk} resolves outside the KB")
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
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
        target = self._abs(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._abs(src), target)

    def walk(self) -> Iterator[str]:
        """Regular files below the root; symlinked files and directories are skipped."""
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(Path(dirpath) / d))
            base = Path(dirpath).relative_to(self.root)
            for name in sorted(filenames):
                if not os.path.islink(Path(dirpath) / name):
                    yield (base / name).as_posix()
