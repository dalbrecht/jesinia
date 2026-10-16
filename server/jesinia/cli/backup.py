# SPDX-License-Identifier: AGPL-3.0-or-later
"""``jesinia backup``: copy what cannot be rebuilt (spec §3.4)."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from jesinia.core.db import DbClass, ModuleDb


def backup(data_dir: Path, dbs: Iterable[ModuleDb], dest: Path | None = None) -> Path:
    """Copy every history-class database (online backup API) and ``config.toml`` into
    ``<dest or data_dir/backups>/<UTC timestamp>/``; return that folder."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = (dest or data_dir / "backups") / stamp
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    for spec in dbs:
        src_path = data_dir / f"{spec.name}.sqlite"
        if spec.db_class is not DbClass.HISTORY or not src_path.exists():
            continue
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        dst = sqlite3.connect(out / src_path.name)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    config = data_dir / "config.toml"
    if config.exists():
        shutil.copy2(config, out / "config.toml")
        (out / "config.toml").chmod(0o600)
    return out
