# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contained, atomic file access under the KB root."""

from __future__ import annotations

import os
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
        path = self._abs(disk)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.jesinia-tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
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
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            base = Path(dirpath).relative_to(self.root)
            for name in sorted(filenames):
                yield (base / name).as_posix()
