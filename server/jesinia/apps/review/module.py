# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Review app module: owns ``review.sqlite``, the repo workspace and the repo tools.
Starts last."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.review.api.tools import build_tools
from jesinia.apps.review.application.repo_tools import RepoTools
from jesinia.apps.review.application.sessions import ReviewSessionRegistry
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
        self._repo_tools: RepoTools | None = None
        # Review jobs bind their agent sessions here; the repo tools read through it.
        self.sessions = ReviewSessionRegistry()

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "review module not started")
        return self._workspace

    @property
    def repo_tools(self) -> RepoTools:
        if self._repo_tools is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "review module not started")
        return self._repo_tools

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        self._conn = open_db(ctx.cfg.data_dir, REVIEW_DB)
        self._workspace = GitWorkspace(ctx.cfg.data_dir / "review" / "repos", github_remote_url)
        self._repo_tools = RepoTools(
            self.sessions.worktree_for, self._workspace, self.sessions.allowance
        )

    async def stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._workspace = None
        self._repo_tools = None

    def health(self) -> ModuleHealth:
        ready = self._workspace is not None
        return ModuleHealth(ready, "ready" if ready else "not started")
