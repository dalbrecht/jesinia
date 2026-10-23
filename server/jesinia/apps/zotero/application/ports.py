# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports the Zotero use cases depend on; infrastructure implements them."""

from __future__ import annotations

from typing import Literal, Protocol

from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.domain.paper import Paper


class KeyStore(Protocol):
    """Where the local API key lives (the macOS Keychain in production)."""

    async def read(self) -> str | None: ...

    async def write(self, key: str) -> None: ...


class ZoteroLibrary(Protocol):
    """The slice of Zotero's local API Jesinia uses. There is deliberately no delete."""

    async def library_version(self) -> int: ...

    async def changed_since(self, version: int) -> tuple[list[dict[str, object]], int]:
        """Raw items changed after ``version`` and the library version they are current to."""
        ...

    async def live_keys(self) -> set[ItemKey]:
        """Every non-trashed key; the local API has no ``/deleted`` endpoint (spec 2a)."""
        ...

    async def trashed_since(self, version: int) -> list[ItemKey]: ...

    async def server_id(self) -> str:
        """The ``Zotero-Server-ID`` response header; writes must echo it."""
        ...

    async def item(self, key: ItemKey) -> dict[str, object]: ...

    async def children(self, key: ItemKey) -> list[dict[str, object]]: ...

    async def fulltext(self, key: ItemKey) -> str | None: ...

    async def fulltext_versions(self, since: int) -> dict[ItemKey, int]: ...

    async def create_items(
        self, items: list[dict[str, object]], *, write_token: str | None = None
    ) -> list[ItemKey]: ...

    async def patch_item(self, key: ItemKey, version: int, fields: dict[str, object]) -> int:
        """Returns the new version; raises ``StaleVersion`` on 412."""
        ...

    async def status(self) -> Literal["connected", "not_running", "api_off", "unauthorized"]: ...


class MirrorStore(Protocol):
    """The local read-only copy of the library, plus the sync cursor and server id."""

    def get(self, key: ItemKey) -> Paper | None: ...

    def put(self, paper: Paper, fulltext: str | None, fulltext_version: int = 0) -> None: ...

    def remove(self, key: ItemKey) -> None: ...

    def clear(self) -> None:
        """Drop every paper and full text and reset the cursor; the server id is kept."""
        ...

    def all_keys(self) -> list[ItemKey]: ...

    def owner_of(self, attachment_key: ItemKey) -> ItemKey | None:
        """The paper whose PDFs include ``attachment_key`` (a standalone PDF owns itself)."""
        ...

    def library_version(self) -> int: ...

    def set_library_version(self, version: int) -> None: ...

    def server_id(self) -> str | None: ...

    def set_server_id(self, server_id: str) -> None: ...

    def fulltext(self, key: ItemKey) -> str | None: ...

    def fulltext_version(self, key: ItemKey) -> int: ...
