# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zotero 10 local API client (spec 2a/3) and the parser from raw items to :class:`Paper`.

The client has no delete capability on purpose: the integration never removes anything from
the author's library. The API key is read from the key store per write and never logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, cast
from urllib.parse import urlparse

import httpx

from jesinia.apps.zotero.application.ports import KeyStore
from jesinia.apps.zotero.domain.errors import (
    AuthorizeTooSoon,
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
_API_PREFIX = "/api"
_USER_PREFIX = f"{_API_PREFIX}/users/0"
_API_OFF_TEXT = "Local API is not enabled"
_PDF = "application/pdf"
_AUTOMATIC_TAG_TYPE = 1
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WRITE_TOKEN_USED_TEXT = "Write token already used"
_MAX_PAGING_ATTEMPTS = 3
# Zotero shows a dialog per authorize call and throttles at 5 a minute (spec 2a).
AUTHORIZE_MIN_INTERVAL_SECONDS = 60.0
# The call blocks until the author answers Zotero's dialog, which can take minutes.
AUTHORIZE_TIMEOUT_SECONDS = 330.0
_APP_NAME = "Jesinia"

RawItem = dict[str, object]


class LocalApiClient:
    """Implements :class:`ZoteroLibrary` over Zotero's local HTTP API."""

    def __init__(
        self,
        base_url: str,
        key_store: KeyStore,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
            # The API key travels in a header, so it must never leave the machine.
            raise ValueError(
                f"Zotero base_url must be http on loopback, got {parsed.scheme}://{parsed.hostname}"
            )
        self._base_url = base_url.rstrip("/")
        self._key_store = key_store
        self._transport = transport
        self._cached_server_id: str | None = None
        self._clock = clock
        self._last_authorize_at: float | None = None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
        json_body: object = None,
        passthrough: tuple[int, ...] = (),
        timeout: float = 30.0,
        user_scoped: bool = True,
    ) -> httpx.Response:
        try:
            # trust_env=False: proxy environment variables must not carry the API key anywhere.
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout, trust_env=False
            ) as http:
                response = await http.request(
                    method,
                    f"{self._base_url}{_USER_PREFIX if user_scoped else _API_PREFIX}{path}",
                    params=params,
                    headers=headers,
                    json=json_body,
                )
        except httpx.ConnectError as error:
            raise ZoteroUnavailable("not_running", str(error)) from error
        except httpx.TransportError as error:
            raise ZoteroUnavailable("not_running", type(error).__name__) from error
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
        for _ in range(_MAX_PAGING_ATTEMPTS):
            collected: list[RawItem] = []
            first_version: int | None = None
            while True:
                response = await self._send(
                    "GET",
                    path,
                    params={
                        "since": since,
                        "limit": PAGE_SIZE,
                        "start": len(collected),
                        "sort": "dateAdded",
                        "direction": "asc",
                    },
                )
                version = int(response.headers.get("Last-Modified-Version", "0"))
                first_version = version if first_version is None else first_version
                page = cast(list[RawItem], response.json())
                collected.extend(page)
                if len(page) < PAGE_SIZE:
                    break
            if version == first_version:
                return collected, first_version
        raise ZoteroError("library kept changing during sync")

    async def authorize(self) -> bool:
        """Ask Zotero for a local API key; store it only if the author chose "Always Allow".

        Returns whether the key was remembered. A single-use key (plain "Allow") is not
        stored, so the caller should tell the author to pick "Always Allow".
        """
        now = self._clock()
        if (
            self._last_authorize_at is not None
            and now - self._last_authorize_at < AUTHORIZE_MIN_INTERVAL_SECONDS
        ):
            raise AuthorizeTooSoon("Zotero authorize was requested less than a minute ago")
        self._last_authorize_at = now
        response = await self._send(
            "POST",
            "/local/authorize",
            headers={"Zotero-Server-ID": await self.server_id()},
            json_body={"appName": _APP_NAME},
            passthrough=(403, 429),
            timeout=AUTHORIZE_TIMEOUT_SECONDS,
            user_scoped=False,
        )
        if response.status_code == 403:
            raise ZoteroUnavailable("unauthorized", "denied")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise AuthorizeTooSoon(f"Zotero throttled authorize; retry after {retry_after}s")
        body = cast(dict[str, object], response.json())
        remember, key = body.get("remember"), body.get("key")
        if not isinstance(remember, bool) or (remember and not (isinstance(key, str) and key)):
            # Never echo the body: it may carry a key.
            raise ZoteroError("unexpected authorize response")
        if remember:
            await self._key_store.write(cast(str, key))
        return remember

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
        if write_token is not None and len(items) > CREATE_BATCH_SIZE:
            # One token is one atomic batch; splitting would make a partial failure unrecoverable.
            raise ValueError(f"a write token covers at most {CREATE_BATCH_SIZE} items")
        keys: list[ItemKey] = []
        for start in range(0, len(items), CREATE_BATCH_SIZE):
            batch = items[start : start + CREATE_BATCH_SIZE]
            extra = {} if write_token is None else {"Zotero-Write-Token": write_token}
            response = await self._send(
                "POST",
                "/items",
                headers=await self._write_headers(extra),
                json_body=batch,
                passthrough=(412,),
            )
            if response.status_code == 412:
                if _WRITE_TOKEN_USED_TEXT in response.text:
                    raise WriteTokenUsed(response.text.strip())
                raise ZoteroError(f"Zotero POST /items returned 412: {response.text[:200]}")
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
