# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Review app module: owns ``review.sqlite`` and the repo workspace. Starts last."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.review.infrastructure.workspace import GitWorkspace, github_remote_url
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef

# History: rounds, drafts, posted reviews and the knowledge model can't be rebuilt from
# GitHub, and `jesinia backup` copies only history databases.
REVIEW_DB = ModuleDb("review", DbClass.HISTORY, files("jesinia.apps.review.migrations"))


class ReviewModule:
    name = "review"

    def __init__(self) -> None:
        self._workspace: GitWorkspace | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "review module not started")
        return self._workspace

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, REVIEW_DB)
        self._workspace = GitWorkspace(ctx.cfg.data_dir / "review" / "repos", github_remote_url)

    async def stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._workspace = None

    def health(self) -> ModuleHealth:
        ready = self._workspace is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
