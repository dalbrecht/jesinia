# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RSS app module. Starts after fetch (every feed request goes through it), and after kb
and agents, which Keep and Engage use."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.rss.api import build_router, build_tools
from jesinia.apps.rss.application.engage import EngageService
from jesinia.apps.rss.application.feeds import FeedService
from jesinia.apps.rss.application.keep import KeepService
from jesinia.apps.rss.application.poller import Poller
from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.domain.settings import (
    DEFAULT_SOURCE_FOLDER,
    SOURCE_FOLDER_KIND,
    InvalidSettings,
    RssSettings,
)
from jesinia.apps.rss.infrastructure.parse import FeedparserReader
from jesinia.apps.rss.infrastructure.store import SqliteFeedStore, SqlitePostStore
from jesinia.core.config import ConfigError, set_values
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.idempotency import RETENTION, IdempotencyStore, SqliteIdempotencyStore
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.fetch.application.service import FetchService
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.application.tags import TagService

log = logging.getLogger(__name__)

# Read state cannot be rebuilt from anywhere, so rss.sqlite is HISTORY (backed up).
RSS_DB = ModuleDb("rss", DbClass.HISTORY, files("jesinia.apps.rss.migrations"))
IDEMPOTENCY_PRUNE_JOB = "rss.idempotency-prune"
POLL_JOB = "rss.poll"
POSTS_PRUNE_JOB = "rss.prune"
# How often the poller looks for due feeds; each feed's own interval is 15 min to 12 h.
POLL_TICK = timedelta(seconds=60)


def _not_started() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "rss module not started")


def _missing(feature: str, service: str) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, f"rss {feature} requires the {service} service"
    )


class RssModule:
    name = "rss"

    def __init__(self, retention: Callable[[], timedelta] | None = None) -> None:
        """``retention`` gives the retention window at each prune; by default it is the
        ``rss.retention`` setting, read from the live config each time."""
        self._retention_override = retention
        self._ctx: AppContext | None = None
        self._svc: FeedService | None = None
        self._posts: PostService | None = None
        self._poller: Poller | None = None
        self._keeper: KeepService | None = None
        self._engage: EngageService | None = None
        self._idempotency: SqliteIdempotencyStore | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def service(self) -> FeedService:
        if self._svc is None:
            raise _not_started()
        return self._svc

    @property
    def posts(self) -> PostService:
        if self._posts is None:
            raise _not_started()
        return self._posts

    @property
    def poller(self) -> Poller:
        if self._poller is None:
            raise _not_started()
        return self._poller

    @property
    def idempotency(self) -> IdempotencyStore:
        if self._idempotency is None:
            raise _not_started()
        return self._idempotency

    @property
    def keeper(self) -> KeepService:
        if self._keeper is None:
            raise _missing("keep", "kb") if self._svc is not None else _not_started()
        return self._keeper

    @property
    def engage(self) -> EngageService:
        if self._engage is None:
            raise _missing("engage", "agents") if self._svc is not None else _not_started()
        return self._engage

    def _context(self) -> AppContext:
        if self._ctx is None:
            raise _not_started()
        return self._ctx

    def _source_folder(self) -> str:
        return self._context().cfg.folders.get(SOURCE_FOLDER_KIND, DEFAULT_SOURCE_FOLDER)

    def settings(self) -> RssSettings:
        """Settings → RSS, from the live config."""
        return RssSettings(self._context().cfg.rss_retention.days, self._source_folder())

    def update_settings(self, settings: RssSettings) -> RssSettings:
        """Save to ``config.toml`` and apply to the live config (the prune job and Keep read
        it each time). Raises ``InvalidSettings`` if the config rejects a value."""
        ctx = self._context()
        values = {
            "rss.retention": f"{settings.retention_days}d",
            f"folders.{SOURCE_FOLDER_KIND}": settings.source_folder,
        }
        try:
            saved = set_values(ctx.cfg.data_dir / "config.toml", values)
        except ConfigError as exc:
            raise InvalidSettings(str(exc)) from exc
        # Only these two settings change in memory; never swap others (e.g. the token).
        folders = {**ctx.cfg.folders, SOURCE_FOLDER_KIND: saved.folders[SOURCE_FOLDER_KIND]}
        ctx.cfg = replace(ctx.cfg, rss_retention=saved.rss_retention, folders=folders)
        return self.settings()

    async def prune_idempotency_keys(self) -> None:
        """Forget idempotency keys older than ``RETENTION`` (a daily job)."""
        if self._idempotency is not None:
            removed = self._idempotency.prune(datetime.now(UTC) - RETENTION)
            log.info("pruned %d expired rss idempotency keys", removed)

    async def prune_posts(self) -> None:
        """Delete unkept posts past the retention window (a daily job)."""
        if self._posts is not None:
            removed = self._posts.prune(datetime.now(UTC))
            log.info("pruned %d rss posts past retention", removed)

    async def poll_due(self) -> None:
        """Poll the feeds that are due (a job every ``POLL_TICK``)."""
        if self._poller is not None:
            summary = await self._poller.poll_due()
            if summary.polled:
                log.info("rss poll: %s", summary)

    def router(self) -> APIRouter | None:
        return build_router(self)

    def mcp_tools(self) -> Sequence[ToolDef]:
        return build_tools(self)

    async def start(self, ctx: AppContext) -> None:
        fetch = ctx.services.get("fetch")
        if not isinstance(fetch, FetchService):
            raise RuntimeError("rss requires the fetch service (start it first)")
        self._ctx = ctx
        self._conn = open_db(ctx.cfg.data_dir, RSS_DB)
        self._idempotency = SqliteIdempotencyStore(self._conn)
        # Single process and nothing served yet: any pending key was left by a crash.
        if cleared := self._idempotency.clear_pending():
            log.info("cleared %d rss idempotency keys left pending by an unclean stop", cleared)
        ctx.scheduler.every(IDEMPOTENCY_PRUNE_JOB, timedelta(days=1), self.prune_idempotency_keys)
        store, reader = SqliteFeedStore(self._conn), FeedparserReader()
        self._svc = FeedService(store, fetch, reader)
        self._poller = Poller(store, fetch, reader, lambda: datetime.now(UTC))
        retention = self._retention_override or (lambda: ctx.cfg.rss_retention)
        posts_store = SqlitePostStore(self._conn)
        self._posts = PostService(posts_store, fetch, retention)
        ctx.scheduler.every(POLL_JOB, POLL_TICK, self.poll_due)
        ctx.scheduler.every(POSTS_PRUNE_JOB, timedelta(days=1), self.prune_posts)
        kb, tags = ctx.services.get("kb"), ctx.services.get("tags")
        if isinstance(kb, KbService) and isinstance(tags, TagService):
            self._keeper = KeepService(
                kb, tags, self._posts, posts_store, store, fetch, fetch, self._source_folder
            )
            if resumed := self._keeper.recover():
                log.info("resumed %d rss archives left running by a stop", resumed)
        sessions = ctx.services.get("agents")
        if isinstance(sessions, SessionService):
            self._engage = EngageService(self._posts, sessions)

    async def stop(self) -> None:
        if self._keeper is not None:
            await self._keeper.stop()
        self._keeper = None
        self._engage = None
        self._svc = None
        self._posts = None
        self._poller = None
        self._idempotency = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        return ModuleHealth(self._svc is not None, "ready" if self._svc else "not started")
