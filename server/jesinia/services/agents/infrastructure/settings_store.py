# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ModelSettingsStore`` over the ``settings`` table in ``sessions.sqlite``."""

from __future__ import annotations

import json
import logging
import sqlite3

from jesinia.services.agents.domain.settings import InvalidModelName, checked_model_name

log = logging.getLogger(__name__)
_PREFIX = "model."


class SqliteModelSettingsStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def models(self) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, json FROM settings WHERE key LIKE ? ORDER BY key", (_PREFIX + "%",)
        ).fetchall()
        out: dict[str, str] = {}
        for key, raw in rows:
            runner = str(key).removeprefix(_PREFIX)
            value = json.loads(raw)
            try:
                if not isinstance(value, str):
                    raise InvalidModelName("the stored model is not text")
                out[runner] = checked_model_name(value)
            except InvalidModelName as exc:
                # Never hand an unsafe stored value to argv; the runner falls back instead.
                log.warning("agents: ignoring %s's saved model: %s", runner, exc)
        return out

    def set_model(self, runner: str, model: str | None) -> None:
        if model is None:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (_PREFIX + runner,))
            return
        self._conn.execute(
            "INSERT INTO settings (key, json) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET json = excluded.json",
            (_PREFIX + runner, json.dumps(model)),
        )
