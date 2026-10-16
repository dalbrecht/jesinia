# SPDX-License-Identifier: AGPL-3.0-or-later
"""The modules a Jesinia server runs, in start order, and the databases they own."""

from __future__ import annotations

from jesinia.core.db import ModuleDb
from jesinia.core.module import Module
from jesinia.services.agents.module import SESSIONS_DB, AgentsModule
from jesinia.services.fetch.module import FETCH_DB, FetchModule
from jesinia.services.github.module import GITHUB_DB, GitHubModule
from jesinia.services.kb.module import KB_DB, KbModule
from jesinia.services.kbsync.module import KbSyncModule
from jesinia.services.search.module import SEARCH_DB, SearchModule


def modules() -> list[Module]:
    """Fresh module instances: kb first (search and kbsync depend on it)."""
    return [
        KbModule(),
        SearchModule(),
        KbSyncModule(),
        AgentsModule(),
        GitHubModule(),
        FetchModule(),
    ]


DATABASES: tuple[ModuleDb, ...] = (KB_DB, SEARCH_DB, SESSIONS_DB, GITHUB_DB, FETCH_DB)
