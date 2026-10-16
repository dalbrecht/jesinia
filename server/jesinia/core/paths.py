# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filesystem locations. ``JESINIA_HOME`` relocates everything (tests, setup smoke)."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "JESINIA_HOME"


def _override() -> Path | None:
    value = os.environ.get(ENV_HOME)
    return Path(value) if value else None


def default_data_dir() -> Path:
    """``~/Library/Application Support/jesinia`` (or ``$JESINIA_HOME``)."""
    override = _override()
    if override is not None:
        return override
    return Path.home() / "Library" / "Application Support" / "jesinia"


def default_log_dir() -> Path:
    """``~/Library/Logs/jesinia`` (or ``$JESINIA_HOME/logs``)."""
    override = _override()
    if override is not None:
        return override / "logs"
    return Path.home() / "Library" / "Logs" / "jesinia"


def config_path() -> Path:
    """``config.toml`` inside the data directory."""
    return default_data_dir() / "config.toml"
