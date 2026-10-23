# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-memory fake of Zotero 10's local API (spec 2a) for tests and end-to-end runs.

Faithful to the spiked behaviour: versions bump on every write, ``If-Unmodified-Since-Version``
is required (428) and checked (412), write tokens are single-use, relations are *not*
reciprocal, and there is no ``/deleted`` endpoint. ``mode`` and ``down`` simulate the
"local API off", "not authorized" and "Zotero not running" states.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from jesinia.apps.zotero.domain.identifiers import ItemKey

PREFIX = "/api/users/0"
WRITE_TOKEN_LENGTH = 32
_Item = dict[str, object]
_Handler = Callable[[Request], Awaitable[Response]]


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeZotero) -> None:
        self._fake = fake
        self._asgi = httpx.ASGITransport(app=fake.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._fake.down:
            raise httpx.ConnectError("connection refused", request=request)
        return await self._asgi.handle_async_request(request)


class FakeZotero:
    """State plus the Starlette ``app``; use :meth:`transport` to talk to it in-process."""

    def __init__(self) -> None:
        self.version = 0
        self.fulltext_version = 0
        self.server_id = "FAKESERVERID0001"
        self.issued_key = "fake-local-api-key-0000000000000"
        self.valid_key: str | None = None
        self.mode = "ok"  # "ok" | "off" | "unauthorized" | "forbidden" | "broken"
        self.down = False
        self.authorize_outcome = "always"  # "always" | "once" | "denied" | "throttled"
        self.authorize_body: object = None
        self.omit_total = False
        self.on_page: Callable[[int], None] | None = None  # called with ``start`` after a page
        self.last_headers: dict[str, str] = {}
        self.writes: list[tuple[str, str, object]] = []
        self._items: dict[str, _Item] = {}
        self._used_tokens: set[str] = set()
        self._counter = 0
        self.app = Starlette(
            routes=[
                Route("/api/local/authorize", self._authorize, methods=["POST"]),
                Route(f"{PREFIX}/items", self._guarded(self._list_items), methods=["GET"]),
                Route(f"{PREFIX}/items", self._guarded(self._create_items), methods=["POST"]),
                Route(f"{PREFIX}/items/trash", self._guarded(self._list_trash), methods=["GET"]),
                Route(f"{PREFIX}/items/{{key}}", self._guarded(self._get_item), methods=["GET"]),
                Route(
                    f"{PREFIX}/items/{{key}}", self._guarded(self._patch_item), methods=["PATCH"]
                ),
                Route(
                    f"{PREFIX}/items/{{key}}/children",
                    self._guarded(self._children),
                    methods=["GET"],
                ),
                Route(
                    f"{PREFIX}/items/{{key}}/fulltext",
                    self._guarded(self._fulltext),
                    methods=["GET"],
                ),
                Route(
                    f"{PREFIX}/fulltext", self._guarded(self._fulltext_versions), methods=["GET"]
                ),
            ]
        )

    def transport(self) -> httpx.AsyncBaseTransport:
        """A transport that raises ``ConnectError`` while :attr:`down` is true."""
        return _FakeTransport(self)

    # ---- helpers for tests ----------------------------------------------------------------

    def add_item(
        self,
        title: str = "",
        *,
        item_type: str = "journalArticle",
        parent: str | None = None,
        **fields: object,
    ) -> ItemKey:
        """Add an item (or child, with ``parent=``) and return its key."""
        self._counter += 1
        key = f"FAKE{self._counter:04d}"
        data: _Item = {"key": key, "itemType": item_type, "title": title, **fields}
        if parent is not None:
            data["parentItem"] = parent
        self._items[key] = {"key": key, "version": self._bump(), "data": data}
        return ItemKey(key)

    def item_version(self, key: str) -> int:
        return cast(int, self._items[key]["version"])

    def item_title(self, key: str) -> str:
        return str(cast(_Item, self._items[key]["data"])["title"])

    def trash(self, key: str) -> None:
        self._items[key]["trashed"] = True
        self._items[key]["version"] = self._bump()

    def remove(self, key: str) -> None:
        """Permanently delete, as emptying the trash in Zotero does."""
        del self._items[key]
        self._bump()

    def set_fulltext(self, key: str, text: str) -> None:
        """Index text for ``key``; bumps only the fulltext version, not the item's."""
        self.fulltext_version += 1
        self._items[key]["fulltext"] = text
        self._items[key]["fulltext_version"] = self.fulltext_version

    def _bump(self) -> int:
        self.version += 1
        return self.version

    # ---- request plumbing -----------------------------------------------------------------

    def _respond(self, request: Request, response: Response, **headers: object) -> Response:
        response.headers["Zotero-Server-ID"] = self.server_id
        for name, value in headers.items():
            response.headers[name.replace("_", "-")] = str(value)
        return response

    def _gate(self, request: Request) -> Response | None:
        self.last_headers = {name.lower(): value for name, value in request.headers.items()}
        if self.mode == "off":
            return PlainTextResponse("Local API is not enabled", status_code=403)
        if self.mode == "unauthorized":
            return PlainTextResponse("Forbidden", status_code=401)
        if self.mode == "forbidden":
            return PlainTextResponse("nope", status_code=403)
        if self.mode == "broken":
            return PlainTextResponse("boom", status_code=500)
        if request.method in ("POST", "PATCH"):
            sent = request.headers.get("zotero-api-key")
            if sent is None or (self.valid_key is not None and sent != self.valid_key):
                return PlainTextResponse("Unauthorized", status_code=401)
        return None

    def _guarded(self, handler: Callable[[Request], Awaitable[Response]]) -> _Handler:
        async def guarded(request: Request) -> Response:
            blocked = self._gate(request)
            return blocked if blocked is not None else await handler(request)

        return guarded

    def _live(self) -> list[_Item]:
        live = [item for item in self._items.values() if not item.get("trashed")]
        return sorted(live, key=lambda item: cast(int, item["version"]))

    async def _authorize(self, request: Request) -> Response:
        self.authorize_body = json.loads(await request.body())
        self.last_headers = {name.lower(): value for name, value in request.headers.items()}
        if "zotero-server-id" not in self.last_headers:
            return PlainTextResponse("Zotero-Server-ID required", status_code=428)
        if self.authorize_outcome == "denied":
            return self._respond(request, JSONResponse({"denied": True}, status_code=403))
        if self.authorize_outcome == "throttled":
            response = PlainTextResponse("slow down", status_code=429)
            response.headers["Retry-After"] = "30"
            return self._respond(request, response)
        remember = self.authorize_outcome == "always"
        return self._respond(request, JSONResponse({"key": self.issued_key, "remember": remember}))

    async def _list_items(self, request: Request) -> Response:
        since = int(request.query_params.get("since", "0"))
        matching = [item for item in self._live() if cast(int, item["version"]) > since]
        if request.query_params.get("format") == "keys":
            body = "\n".join(str(item["key"]) for item in self._live())
            return self._respond(
                request, PlainTextResponse(body), Last_Modified_Version=self.version
            )
        return self._page(request, matching)

    async def _list_trash(self, request: Request) -> Response:
        since = int(request.query_params.get("since", "0"))
        trashed = [
            item
            for item in self._items.values()
            if item.get("trashed") and cast(int, item["version"]) > since
        ]
        trashed.sort(key=lambda item: cast(int, item["version"]))
        return self._page(request, trashed)

    def _page(self, request: Request, items: list[_Item]) -> Response:
        limit = int(request.query_params.get("limit", "25"))
        start = int(request.query_params.get("start", "0"))
        body = [self._public(item) for item in items[start : start + limit]]
        response = self._respond(request, JSONResponse(body))
        if not self.omit_total:
            response.headers["Total-Results"] = str(len(items))
        response.headers["Last-Modified-Version"] = str(self.version)
        if self.on_page is not None:
            self.on_page(start)
        return response

    @staticmethod
    def _public(item: _Item) -> _Item:
        return {"key": item["key"], "version": item["version"], "data": item["data"]}

    async def _get_item(self, request: Request) -> Response:
        item = self._items.get(request.path_params["key"])
        if item is None:
            return self._respond(request, PlainTextResponse("Not found", status_code=404))
        return self._respond(
            request, JSONResponse(self._public(item)), Last_Modified_Version=self.version
        )

    async def _children(self, request: Request) -> Response:
        key = request.path_params["key"]
        children = [
            item for item in self._live() if cast(_Item, item["data"]).get("parentItem") == key
        ]
        return self._page(request, children)

    async def _fulltext(self, request: Request) -> Response:
        item = self._items.get(request.path_params["key"])
        if item is None or "fulltext" not in item:
            return self._respond(request, PlainTextResponse("", status_code=404))
        text = str(item["fulltext"])
        body = {"content": text, "indexedPages": 1, "totalPages": 1}
        return self._respond(request, JSONResponse(body))

    async def _fulltext_versions(self, request: Request) -> Response:
        since = int(request.query_params.get("since", "0"))
        versions = {
            str(item["key"]): item["fulltext_version"]
            for item in self._items.values()
            if cast(int, item.get("fulltext_version", 0)) > since
        }
        return self._respond(request, JSONResponse(versions))

    async def _create_items(self, request: Request) -> Response:
        payload = cast(list[_Item], json.loads(await request.body()))
        self.writes.append(("POST", request.url.path, payload))
        token = request.headers.get("zotero-write-token")
        if token is not None:
            if len(token) != WRITE_TOKEN_LENGTH:
                return self._respond(
                    request, PlainTextResponse("Invalid write token", status_code=400)
                )
            if token in self._used_tokens:
                return self._respond(
                    request, PlainTextResponse("Write token already used", status_code=412)
                )
            self._used_tokens.add(token)
        successful: dict[str, _Item] = {}
        failed: dict[str, _Item] = {}
        for index, fields in enumerate(payload):
            if "itemType" not in fields:
                failed[str(index)] = {"code": 400, "message": "'itemType' property not provided"}
                continue
            data = {key: value for key, value in fields.items() if key != "itemType"}
            parent = fields.get("parentItem")
            key = self.add_item(
                str(data.pop("title", "")),
                item_type=str(fields["itemType"]),
                parent=None if parent is None else str(parent),
                **{name: value for name, value in data.items() if name != "parentItem"},
            )
            successful[str(index)] = self._public(self._items[key])
        body = {
            "successful": successful,
            "success": {index: item["key"] for index, item in successful.items()},
            "unchanged": {},
            "failed": failed,
        }
        return self._respond(request, JSONResponse(body), Last_Modified_Version=self.version)

    async def _patch_item(self, request: Request) -> Response:
        key = request.path_params["key"]
        fields = cast(_Item, json.loads(await request.body()))
        self.writes.append(("PATCH", request.url.path, fields))
        item = self._items.get(key)
        if item is None:
            return self._respond(request, PlainTextResponse("Not found", status_code=404))
        expected = request.headers.get("if-unmodified-since-version")
        if expected is None:
            return self._respond(
                request, PlainTextResponse("Precondition required", status_code=428)
            )
        if int(expected) != item["version"]:
            return self._respond(request, PlainTextResponse("Item changed", status_code=412))
        cast(_Item, item["data"]).update(fields)
        item["version"] = self._bump()
        return self._respond(request, Response(status_code=204), Last_Modified_Version=self.version)
