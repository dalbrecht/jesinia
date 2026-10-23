# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zotero 10 local API client (spec 2a/3) and the parser from raw items to :class:`Paper`.

The client has no delete capability on purpose: the integration never removes anything from
the author's library. The API key is read from the key store per write and never logged.
"""

from __future__ import annotations

from typing import Literal, cast

import httpx

from jesinia.apps.zotero.application.ports import KeyStore
from jesinia.apps.zotero.domain.errors import (
    StaleVersion,
    WriteTokenUsed,
    ZoteroError,
    ZoteroUnavailable,
)
from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.domain.paper import (
    Annotation,
    Attachment,
    Creator,
    Note,
    Paper,
    ZoteroTag,
)

PAGE_SIZE = 100
CREATE_BATCH_SIZE = 50
_USER_PREFIX = "/users/0"
_API_OFF_TEXT = "Local API is not enabled"
_PDF = "application/pdf"
_AUTOMATIC_TAG_TYPE = 1

RawItem = dict[str, object]


class LocalApiClient:
    """Implements :class:`ZoteroLibrary` over Zotero's local HTTP API."""

    def __init__(
        self,
        base_url: str,
        key_store: KeyStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_store = key_store
        self._transport = transport
        self._cached_server_id: str | None = None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
        json_body: object = None,
        passthrough: tuple[int, ...] = (),
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as http:
                response = await http.request(
                    method,
                    f"{self._base_url}{_USER_PREFIX}{path}",
                    params=params,
                    headers=headers,
                    json=json_body,
                )
        except httpx.ConnectError as error:
            raise ZoteroUnavailable("not_running", str(error)) from error
        server_id = response.headers.get("Zotero-Server-ID")
        if server_id:
            self._cached_server_id = server_id
        if response.status_code == 403 and _API_OFF_TEXT in response.text:
            raise ZoteroUnavailable("api_off")
        if response.status_code == 401:
            raise ZoteroUnavailable("unauthorized")
        if response.status_code >= 400 and response.status_code not in passthrough:
            raise ZoteroError(
                f"Zotero {method} {path} returned {response.status_code}: {response.text[:200]}"
            )
        return response

    async def _write_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        key = await self._key_store.read()
        if key is None:
            raise ZoteroUnavailable("unauthorized", "no local API key stored")
        headers = {"Zotero-API-Key": key, "Zotero-Server-ID": await self.server_id()}
        return headers | (extra or {})

    async def _paged(self, path: str, since: int) -> tuple[list[RawItem], int]:
        collected: list[RawItem] = []
        version = 0
        start = 0
        while True:
            response = await self._send(
                "GET", path, params={"since": since, "limit": PAGE_SIZE, "start": start}
            )
            if start == 0:
                version = int(response.headers.get("Last-Modified-Version", "0"))
            page = cast(list[RawItem], response.json())
            collected.extend(page)
            start += len(page)
            if not page or start >= int(response.headers.get("Total-Results", "0")):
                return collected, version

    async def library_version(self) -> int:
        response = await self._send("GET", "/items", params={"limit": 1})
        return int(response.headers.get("Last-Modified-Version", "0"))

    async def changed_since(self, version: int) -> tuple[list[dict[str, object]], int]:
        return await self._paged("/items", version)

    async def live_keys(self) -> set[ItemKey]:
        response = await self._send("GET", "/items", params={"format": "keys"})
        return {ItemKey(line) for line in response.text.splitlines() if line}

    async def trashed_since(self, version: int) -> list[ItemKey]:
        items, _ = await self._paged("/items/trash", version)
        return [ItemKey(str(item["key"])) for item in items]

    async def server_id(self) -> str:
        if self._cached_server_id is None:
            await self._send("GET", "/items", params={"limit": 1})
        if self._cached_server_id is None:
            raise ZoteroError("Zotero did not send a Zotero-Server-ID header")
        return self._cached_server_id

    async def item(self, key: ItemKey) -> dict[str, object]:
        return cast(RawItem, (await self._send("GET", f"/items/{key}")).json())

    async def children(self, key: ItemKey) -> list[dict[str, object]]:
        items, _ = await self._paged(f"/items/{key}/children", 0)
        return items

    async def fulltext(self, key: ItemKey) -> str | None:
        response = await self._send("GET", f"/items/{key}/fulltext", passthrough=(404,))
        if response.status_code == 404:
            return None
        return str(cast(dict[str, object], response.json())["content"])

    async def fulltext_versions(self, since: int) -> dict[ItemKey, int]:
        response = await self._send("GET", "/fulltext", params={"since": since})
        return {ItemKey(key): int(value) for key, value in response.json().items()}

    async def create_items(
        self, items: list[dict[str, object]], *, write_token: str | None = None
    ) -> list[ItemKey]:
        keys: list[ItemKey] = []
        for batch_number, start in enumerate(range(0, len(items), CREATE_BATCH_SIZE)):
            batch = items[start : start + CREATE_BATCH_SIZE]
            extra: dict[str, str] = {}
            if write_token is not None:
                # A token is single-use, so later batches of one logical create get their own.
                extra["Zotero-Write-Token"] = (
                    write_token if batch_number == 0 else f"{write_token}-{batch_number}"
                )
            response = await self._send(
                "POST",
                "/items",
                headers=await self._write_headers(extra),
                json_body=batch,
                passthrough=(412,),
            )
            if response.status_code == 412:
                raise WriteTokenUsed(response.text.strip())
            outcome = cast(dict[str, object], response.json())
            if outcome.get("failed"):
                raise ZoteroError(
                    f"Zotero rejected some created items; failed: {outcome['failed']}"
                )
            created = cast(dict[str, str], outcome["success"])
            keys.extend(ItemKey(created[index]) for index in sorted(created, key=int))
        return keys

    async def patch_item(self, key: ItemKey, version: int, fields: dict[str, object]) -> int:
        headers = await self._write_headers({"If-Unmodified-Since-Version": str(version)})
        response = await self._send(
            "PATCH", f"/items/{key}", headers=headers, json_body=fields, passthrough=(412,)
        )
        if response.status_code == 412:
            raise StaleVersion(f"item {key} changed since version {version}")
        return int(response.headers["Last-Modified-Version"])

    async def status(self) -> Literal["connected", "not_running", "api_off", "unauthorized"]:
        try:
            await self.library_version()
        except ZoteroUnavailable as unavailable:
            return unavailable.state
        return "connected"


def _data(raw: RawItem) -> dict[str, object]:
    return cast(dict[str, object], raw["data"])


def _text(data: dict[str, object], field: str) -> str:
    return str(data.get(field) or "")


def _is_pdf(data: dict[str, object]) -> bool:
    return data.get("itemType") == "attachment" and data.get("contentType") == _PDF


def _annotation(raw: RawItem) -> Annotation:
    data = _data(raw)
    return Annotation(
        key=ItemKey(str(raw["key"])),
        text=_text(data, "annotationText"),
        comment=_text(data, "annotationComment"),
        page_label=_text(data, "annotationPageLabel"),
        color=_text(data, "annotationColor"),
    )


def _attachment(raw: RawItem, annotations: tuple[Annotation, ...]) -> Attachment:
    data = _data(raw)
    return Attachment(
        key=ItemKey(str(raw["key"])),
        title=_text(data, "title"),
        content_type=_text(data, "contentType"),
        filename=_text(data, "filename"),
        url=_text(data, "url"),
        annotations=annotations,
    )


def _related(data: dict[str, object]) -> tuple[ItemKey, ...]:
    relations = cast(dict[str, object], data.get("relations") or {})
    value = relations.get("dc:relation") or []
    uris = [value] if isinstance(value, str) else cast(list[str], value)
    return tuple(ItemKey(uri.rsplit("/", 1)[-1]) for uri in uris)


def _creator(raw: dict[str, str]) -> Creator:
    if "name" in raw:
        return Creator(first="", last=raw["name"], kind=raw.get("creatorType", ""))
    return Creator(
        first=raw.get("firstName", ""),
        last=raw.get("lastName", ""),
        kind=raw.get("creatorType", ""),
    )


def parse_paper(raw: dict[str, object], children: list[dict[str, object]]) -> Paper:
    """Build a :class:`Paper` from a raw item and its children.

    ``children`` is the flat list of the item's notes and attachments plus the annotations of
    those attachments (matched to them by ``parentItem``). A parentless PDF becomes a
    standalone paper whose only attachment is itself, with ``children`` as its annotations.
    """
    data = _data(raw)
    annotations_by_parent: dict[str, list[Annotation]] = {}
    for child in children:
        child_data = _data(child)
        if child_data.get("itemType") == "annotation":
            parent = str(child_data.get("parentItem", ""))
            annotations_by_parent.setdefault(parent, []).append(_annotation(child))
    key = str(raw["key"])
    standalone = _is_pdf(data) and not data.get("parentItem")
    attachments: tuple[Attachment, ...]
    notes: tuple[Note, ...]
    if standalone:
        attachments = (_attachment(raw, tuple(annotations_by_parent.get(key, []))),)
        notes = ()
    else:
        attachments = tuple(
            _attachment(child, tuple(annotations_by_parent.get(str(child["key"]), [])))
            for child in children
            if _is_pdf(_data(child))
        )
        notes = tuple(
            Note(key=ItemKey(str(child["key"])), html=_text(_data(child), "note"))
            for child in children
            if _data(child).get("itemType") == "note"
        )
    return Paper(
        key=ItemKey(key),
        version=int(cast(int, raw["version"])),
        item_type=_text(data, "itemType"),
        title=_text(data, "title"),
        creators=tuple(_creator(c) for c in cast(list[dict[str, str]], data.get("creators") or [])),
        date=_text(data, "date"),
        abstract=_text(data, "abstractNote"),
        doi=_text(data, "DOI"),
        url=_text(data, "url"),
        extra=_text(data, "extra"),
        tags=tuple(
            ZoteroTag(name=str(tag["tag"]), automatic=tag.get("type") == _AUTOMATIC_TAG_TYPE)
            for tag in cast(list[dict[str, object]], data.get("tags") or [])
        ),
        related=_related(data),
        notes=notes,
        attachments=attachments,
        standalone=standalone,
    )
