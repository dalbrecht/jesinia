# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Zotero app module. Starts after search: papers are mirrored locally and registered
there. Zotero being down never stops the module from starting."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from importlib.resources import files

from fastapi import APIRouter, HTTPException, status

from jesinia.apps.zotero.application.ports import MirrorStore, ZoteroLibrary
from jesinia.apps.zotero.application.sync import SyncService
from jesinia.apps.zotero.domain.errors import ZoteroUnavailable
from jesinia.apps.zotero.infrastructure.keychain import KeychainKeyStore
from jesinia.apps.zotero.infrastructure.local_api import LocalApiClient, parse_paper
from jesinia.apps.zotero.infrastructure.mirror_store import SqliteMirrorStore
from jesinia.core.config import Config
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.search.application.service import SearchService

log = logging.getLogger(__name__)

# The mirror is a copy of Zotero, rebuilt by resyncing from version 0.
ZOTERO_MIRROR_DB = ModuleDb(
    "zotero_mirror", DbClass.DERIVED, files("jesinia.apps.zotero.migrations.mirror")
)
SYNC_JOB = "zotero.sync"

LibraryFactory = Callable[[Config], ZoteroLibrary]


def _not_started() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "zotero module not started")


def _default_library(cfg: Config) -> ZoteroLibrary:
    return LocalApiClient(cfg.zotero.base_url, KeychainKeyStore())


class ZoteroModule:
    name = "zotero"

    def __init__(self, library_factory: LibraryFactory | None = None) -> None:
        """``library_factory`` builds the Zotero client from the config; tests inject a client
        over the fake Zotero. The default reads the key from the Keychain."""
        self._library_factory = library_factory or _default_library
        self._library: ZoteroLibrary | None = None
        self._mirror: SqliteMirrorStore | None = None
        self._sync: SyncService | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def library(self) -> ZoteroLibrary:
        if self._library is None:
            raise _not_started()
        return self._library

    @property
    def mirror(self) -> MirrorStore:
        if self._mirror is None:
            raise _not_started()
        return self._mirror

    @property
    def sync_service(self) -> SyncService:
        if self._sync is None:
            raise _not_started()
        return self._sync

    async def sync_now(self) -> None:
        """Run one sync pass. Errors are logged with their cause, never raised: sync is a
        best-effort subsystem and must not break its caller."""
        if self._sync is None:
            return
        try:
            report = await self._sync.sync()
        except ZoteroUnavailable as unavailable:
            log.warning("zotero sync skipped: %s", unavailable)
        except Exception:
            log.exception("zotero sync failed")
        else:
            if report.changed or report.removed or report.fulltext_updated:
                log.info("zotero sync: %s", report)

    def router(self) -> APIRouter | None:
        return None

    def mcp_tools(self) -> Sequence[ToolDef]:
        return []

    async def start(self, ctx: AppContext) -> None:
        search = ctx.services.get("search")
        if not isinstance(search, SearchService):
            raise RuntimeError("zotero requires the search service (start it first)")
        self._conn = open_db(ctx.cfg.data_dir, ZOTERO_MIRROR_DB)
        self._mirror = SqliteMirrorStore(self._conn)
        self._library = self._library_factory(ctx.cfg)
        self._sync = SyncService(self._library, self._mirror, search, parse=parse_paper)
        await self._sync.reregister_all()
        ctx.scheduler.every(SYNC_JOB, ctx.cfg.zotero.sync_interval, self.sync_now, at_start=True)

    async def stop(self) -> None:
        self._sync = None
        self._library = None
        self._mirror = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def health(self) -> ModuleHealth:
        return ModuleHealth(self._sync is not None, "ready" if self._sync else "not started")
