# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Google Reader API NetNewsWire's FreshRSS account speaks, at ``/api/greader``
(NetNewsWire sync spec §4, ADR 0012).

Mounted outside the owner-auth dependency: these routes authenticate with the reader
password (``ClientLogin``) and the reader token it returns
(``Authorization: GoogleLogin auth=…``), and that token reaches nothing else. Writes also
need the ``T`` write token. Bodies are form-encoded with repeated keys (``i=…&i=…``), so
they are parsed here rather than by a model.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from jesinia.apps.rss.application.reader import ReaderService
from jesinia.apps.rss.domain.feed import (
    FeedNotFound,
    FeedUnavailable,
    FolderNotFound,
    InvalidFeedUrl,
    RssError,
)
from jesinia.apps.rss.domain.post import PostNotFound
from jesinia.apps.rss.domain.reader import (
    ITEM_IDS_MAX,
    READING_LIST,
    STARRED,
    InvalidStream,
    ItemQuery,
    ReaderAuthFailed,
    ReaderItem,
    StarUnavailable,
    StateStream,
    WriteTokenRejected,
    feed_stream_id,
    item_long_id,
    label_id,
    parse_item_id,
    parse_stream,
    write_token,
)
from jesinia.services.kb.application.errors import KbError, KbUnavailable

log = logging.getLogger(__name__)
PREFIX = "/api/greader"
DEFAULT_IDS = 20
# Form bodies carry at most ~1000 item ids (NetNewsWire's chunk); OPML as in the reader.
FORM_MAX = 256_000
OPML_MAX = 5_000_000
_STATUS: dict[type[RssError], int] = {
    ReaderAuthFailed: status.HTTP_401_UNAUTHORIZED,
    # NetNewsWire fetches a fresh write token and retries once on a 401.
    WriteTokenRejected: status.HTTP_401_UNAUTHORIZED,
    StarUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    FeedNotFound: status.HTTP_404_NOT_FOUND,
    FolderNotFound: status.HTTP_404_NOT_FOUND,
    PostNotFound: status.HTTP_404_NOT_FOUND,
}


class BodyTooLarge(RssError):  # noqa: N818 - the error code clients see
    """A request body over the Reader API's cap."""


_STATUS[BodyTooLarge] = status.HTTP_413_CONTENT_TOO_LARGE


async def _body(request: Request, cap: int) -> str:
    """The body as text, read no further than ``cap`` bytes. Raises ``BodyTooLarge``."""
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > cap:
        raise BodyTooLarge(f"the body is over {cap} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise BodyTooLarge(f"the body is over {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


class ReaderHost(Protocol):
    @property
    def reader(self) -> ReaderService: ...


Params = list[tuple[str, str]]


async def _params(request: Request) -> Params:
    """The query and a form-encoded body, keeping repeated keys in order."""
    params = list(request.query_params.multi_items())
    if request.method == "POST" and "xml" not in request.headers.get("content-type", ""):
        params += parse_qsl(await _body(request, FORM_MAX), True)
    return params


def _one(params: Params, key: str) -> str | None:
    return next((value for name, value in params if name == key), None)


def _all(params: Params, key: str) -> list[str]:
    return [value for name, value in params if name == key]


def _reader_token(request: Request) -> str:
    scheme, _, rest = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "googlelogin" or not rest.strip().startswith("auth="):
        raise ReaderAuthFailed("expected Authorization: GoogleLogin auth=<token>")
    return rest.strip().removeprefix("auth=")


def _text(body: str, code: int = status.HTTP_200_OK) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=code)


def _error(exc: RssError | KbError) -> Response:
    if isinstance(exc, RssError):
        code = _STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST)
    else:  # a Keep the KB refused: unavailable, or conflicting with its state
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if isinstance(exc, KbUnavailable)
            else status.HTTP_409_CONFLICT
        )
    if code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        log.warning("reader api: %s: %s", type(exc).__name__, exc)
    return _text(f"Error={type(exc).__name__}\n{exc}\n", code)


async def _guarded(work: Callable[[], Awaitable[Response]]) -> Response:
    try:
        return await work()
    except (RssError, KbError) as exc:
        return _error(exc)


def _item(reader: ReaderService, item: ReaderItem) -> dict[str, object]:
    post = item.post
    published = post.published_at or post.first_seen_at
    seen_usec = int(post.first_seen_at.timestamp() * 1_000_000)
    categories = [READING_LIST]
    if post.read:
        categories.append("user/-/state/com.google/read")
    if item.is_starred:
        categories.append(STARRED)
    if item.folder is not None:
        categories.append(label_id(item.folder))
    body: dict[str, object] = {
        "id": item_long_id(post.id),
        "crawlTimeMsec": str(seen_usec // 1000),
        "timestampUsec": str(seen_usec),
        "published": int(published.timestamp()),
        "updated": int(published.timestamp()),
        "title": post.title,
        "canonical": [{"href": post.url}],
        "alternate": [{"href": post.url, "type": "text/html"}],
        "summary": {"direction": "ltr", "content": reader.content(item)},
        "categories": categories,
        "origin": {
            "streamId": feed_stream_id(post.feed_id),
            "title": post.feed_title,
            "htmlUrl": item.site_url or "",
        },
    }
    if post.author:
        body["author"] = post.author
    return body


def build_reader_router(host: ReaderHost) -> APIRouter:
    r = APIRouter(prefix=PREFIX, tags=["reader-api"], include_in_schema=False)

    def authed(request: Request) -> str:
        """The caller's reader token; raises ``ReaderAuthFailed`` (answered as 401)."""
        token = _reader_token(request)
        host.reader.authenticate(token)
        return token

    async def writing(request: Request) -> tuple[str, Params]:
        token = authed(request)
        params = await _params(request)
        host.reader.check_write_token(token, _one(params, "T") or "")
        return token, params

    def route(
        path: str, methods: list[str], handler: Callable[[Request], Awaitable[Response]]
    ) -> None:
        async def endpoint(request: Request) -> Response:
            return await _guarded(lambda: handler(request))

        r.add_api_route(path, endpoint, methods=methods, response_class=Response)

    async def client_login(request: Request) -> Response:
        # The password only from the POST body: a query string ends up in logs and history.
        body = parse_qsl(await _body(request, FORM_MAX), True)
        token = host.reader.login(_one(body, "Passwd") or "")
        return _text(f"SID={token}\nLSID=null\nAuth={token}\n")

    async def token(request: Request) -> Response:
        return _text(write_token(authed(request)))

    async def user_info(request: Request) -> Response:
        authed(request)
        return JSONResponse(
            {"userId": "1", "userName": "jesinia", "userProfileId": "1", "userEmail": ""}
        )

    async def tag_list(request: Request) -> Response:
        authed(request)
        tags: list[dict[str, str]] = [{"id": STARRED}]
        tags += [{"id": label_id(name), "type": "folder"} for name in host.reader.folders()]
        return JSONResponse({"tags": tags})

    async def subscription_list(request: Request) -> Response:
        authed(request)
        subscriptions = [
            {
                "id": feed_stream_id(view.id),
                "title": view.title,
                "categories": (
                    [{"id": label_id(view.folder), "label": view.folder}] if view.folder else []
                ),
                "url": view.url,
                "htmlUrl": site_url or "",
                "iconUrl": "",
            }
            for view, site_url in host.reader.subscriptions()
        ]
        return JSONResponse({"subscriptions": subscriptions})

    async def item_ids(request: Request) -> Response:
        authed(request)
        params = await _params(request)
        stream = parse_stream(_one(params, "s") or READING_LIST)
        excluded = _one(params, "xt")
        exclude = parse_stream(excluded) if excluded else None
        if exclude is not None and not isinstance(exclude, StateStream):
            raise InvalidStream("xt must be a state, e.g. user/-/state/com.google/read")
        oldest = _one(params, "ot")
        after = _one(params, "c")
        try:
            limit = min(max(int(_one(params, "n") or DEFAULT_IDS), 1), ITEM_IDS_MAX)
            query = ItemQuery(
                stream,
                exclude.state if exclude is not None else None,
                float(oldest) if oldest else None,
                limit,
                int(after) if after else None,
            )
        except ValueError as exc:
            raise InvalidStream(f"n, ot and c must be numbers ({exc})") from exc
        ids, more = host.reader.item_ids(query)
        body: dict[str, object] = {"itemRefs": [{"id": str(i)} for i in ids]}
        if more is not None:
            body["continuation"] = str(more)
        return JSONResponse(body)

    async def item_contents(request: Request) -> Response:
        authed(request)
        params = await _params(request)
        ids = [parse_item_id(value) for value in _all(params, "i")]
        items = host.reader.items(ids)
        return JSONResponse(
            {
                "id": READING_LIST,
                "updated": int(datetime.now(UTC).timestamp()),
                "items": [_item(host.reader, item) for item in items],
            }
        )

    async def edit_tag(request: Request) -> Response:
        _, params = await writing(request)
        ids = [parse_item_id(value) for value in _all(params, "i")]
        await host.reader.edit_tags(ids, _all(params, "a"), _all(params, "r"))
        return _text("OK")

    async def quick_add(request: Request) -> Response:
        _, params = await writing(request)
        url = _one(params, "quickadd") or ""
        try:
            view = await host.reader.quick_add(url.removeprefix("feed/"))
        except (InvalidFeedUrl, FeedUnavailable) as exc:
            # NetNewsWire reads numResults 0 as "no feed found there".
            return JSONResponse({"numResults": 0, "query": url, "error": str(exc)})
        return JSONResponse(
            {
                "numResults": 1,
                "query": url,
                "streamId": feed_stream_id(view.id),
                "streamName": view.title,
            }
        )

    async def subscription_edit(request: Request) -> Response:
        _, params = await writing(request)
        await host.reader.edit_subscription(
            _one(params, "s") or "",
            _one(params, "ac") or "edit",
            _one(params, "t"),
            _one(params, "a"),
            _one(params, "r"),
        )
        return _text("OK")

    async def rename_tag(request: Request) -> Response:
        _, params = await writing(request)
        host.reader.rename_folder(_one(params, "s") or "", _one(params, "dest") or "")
        return _text("OK")

    async def disable_tag(request: Request) -> Response:
        _, params = await writing(request)
        host.reader.delete_folder(_one(params, "s") or "")
        return _text("OK")

    async def subscription_import(request: Request) -> Response:
        # NetNewsWire sends the OPML as the body, with no write token.
        authed(request)
        await host.reader.import_opml(await _body(request, OPML_MAX))
        return _text("OK")

    route("/accounts/ClientLogin", ["POST"], client_login)
    route("/reader/api/0/token", ["GET"], token)
    route("/reader/api/0/user-info", ["GET"], user_info)
    route("/reader/api/0/tag/list", ["GET"], tag_list)
    route("/reader/api/0/subscription/list", ["GET"], subscription_list)
    route("/reader/api/0/stream/items/ids", ["GET"], item_ids)
    route("/reader/api/0/stream/items/contents", ["GET", "POST"], item_contents)
    route("/reader/api/0/edit-tag", ["POST"], edit_tag)
    route("/reader/api/0/subscription/quickadd", ["POST"], quick_add)
    route("/reader/api/0/subscription/edit", ["POST"], subscription_edit)
    route("/reader/api/0/rename-tag", ["POST"], rename_tag)
    route("/reader/api/0/disable-tag", ["POST"], disable_tag)
    route("/reader/api/0/subscription/import", ["POST"], subscription_import)
    return r
