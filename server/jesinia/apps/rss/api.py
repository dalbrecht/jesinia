# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSS routes under ``/api/rss`` (feeds, folders, OPML, posts, Keep, Engage, settings) and the
read-only MCP tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from jesinia.apps.rss.application.engage import (
    EngageBusy,
    EngageService,
    EngageSessionNotFound,
    NotAnEngageSession,
    NotThisPostsSession,
    TakeawayUnavailable,
)
from jesinia.apps.rss.application.feeds import FeedService, ImportResult
from jesinia.apps.rss.application.keep import KeepService
from jesinia.apps.rss.application.poller import Poller, PollSummary
from jesinia.apps.rss.application.posts import PostService
from jesinia.apps.rss.application.reader import ReaderService
from jesinia.apps.rss.domain.feed import (
    FOLDER_NAME_MAX,
    FeedExists,
    FeedNotFound,
    FeedUnavailable,
    FeedView,
    Folder,
    FolderExists,
    FolderNotFound,
    RssError,
)
from jesinia.apps.rss.domain.kept import ArchiveNotFailed, PostNotKept, Takeaway
from jesinia.apps.rss.domain.post import (
    PAGE_SIZE,
    PAGE_SIZE_MAX,
    ArticleUnavailable,
    Counts,
    PostNotFound,
    PostOutOfScope,
    PostView,
    View,
    format_cursor,
    parse_cursor,
    parse_timestamp,
)
from jesinia.apps.rss.domain.settings import (
    RETENTION_DAYS_MAX,
    RETENTION_DAYS_MIN,
    SOURCE_FOLDER_MAX,
    Retention,
    RssSettings,
    validate_retention,
    validate_settings,
)
from jesinia.core.idempotency import IdempotencyStore, IdempotentCall, idempotent
from jesinia.core.module import CallContext, ToolDef
from jesinia.services.agents.application.packs import UntrustedDoc, wrap_untrusted
from jesinia.services.agents.domain.session import Session
from jesinia.services.kb.api.schemas import TAG_MAX
from jesinia.services.kb.application.errors import (
    InvalidAssetName,
    InvalidTag,
    KbError,
    KbUnavailable,
    NearDuplicate,
    PageNotFound,
    PathOutsideKb,
    ReservedPath,
)

URL_MAX = 2048
OPML_MAX = 5_000_000
OPML_MEDIA_TYPE = "text/x-opml"
QUERY_MAX = 200
CURSOR_MAX = 100
TIMESTAMP_MAX = 64
MAX_READ_IDS = 100_000
NOTE_MAX = 20_000
TAKEAWAY_MAX = 2_000
TAGS_MAX = 20
SESSION_ID_MAX = 100
RUNNER_MAX = 64


class RssHost(Protocol):
    """What the routes need from the module: the live service and the idempotency store."""

    @property
    def service(self) -> FeedService: ...

    @property
    def posts(self) -> PostService: ...

    @property
    def poller(self) -> Poller: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...

    @property
    def keeper(self) -> KeepService: ...

    @property
    def engage(self) -> EngageService: ...

    @property
    def reader(self) -> ReaderService: ...

    def reader_server_url(self) -> str:
        """The server address NetNewsWire's FreshRSS account needs."""
        ...

    def settings(self) -> RssSettings: ...

    def update_settings(self, settings: RssSettings) -> RssSettings:
        """Persist to ``config.toml`` and apply to the live config; ``InvalidSettings``."""
        ...


class ReaderSetupOut(BaseModel):
    """What NetNewsWire's FreshRSS account needs (NetNewsWire sync spec §7)."""

    has_password: bool
    server_url: str


class ReaderPasswordOut(ReaderSetupOut):
    password: str = Field(description="Shown once; only its hash is kept.")


class FeedIn(BaseModel):
    url: str = Field(
        min_length=1, max_length=URL_MAX, description="A feed URL, or a site that links one"
    )
    folder: str | None = Field(default=None, max_length=FOLDER_NAME_MAX)


class FeedPatchIn(BaseModel):
    folder: str | None = Field(max_length=FOLDER_NAME_MAX, description="The folder; null for none")


class FolderPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=FOLDER_NAME_MAX)


class OpmlIn(BaseModel):
    opml: str = Field(min_length=1, max_length=OPML_MAX, description="The OPML document text")


class RetentionOut(BaseModel):
    unit: Literal["days", "posts"]
    value: int

    @classmethod
    def of(cls, retention: Retention) -> RetentionOut:
        return cls(unit=retention.unit, value=retention.value)


class RetentionIn(BaseModel):
    retention: RetentionOut | None = Field(
        description="The feed's own retention (1-365 days, or 10-10000 posts); null for the "
        "global setting"
    )


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


class FeedOut(BaseModel):
    id: int
    title: str
    url: str
    folder: str | None
    folder_id: int | None
    unread: int
    warning: str | None
    last_ok_at: str | None
    fail_count: int
    last_error: str | None
    newest_post_at: str | None
    retention: RetentionOut | None

    @classmethod
    def of(cls, view: FeedView) -> FeedOut:
        return cls(
            id=view.id,
            title=view.title,
            url=view.url,
            folder=view.folder,
            folder_id=view.folder_id,
            unread=view.unread,
            warning=view.warning,
            last_ok_at=_iso(view.last_ok_at),
            fail_count=view.fail_count,
            last_error=view.last_error,
            newest_post_at=_iso(view.newest_post_at),
            retention=None if view.retention is None else RetentionOut.of(view.retention),
        )


class FolderOut(BaseModel):
    id: int
    name: str

    @classmethod
    def of(cls, folder: Folder) -> FolderOut:
        return cls(id=folder.id, name=folder.name)


class ImportFailureOut(BaseModel):
    url: str
    cause: str


class ImportResultOut(BaseModel):
    added: int
    existing: int
    failed: list[ImportFailureOut]

    @classmethod
    def of(cls, result: ImportResult) -> ImportResultOut:
        failed = [ImportFailureOut(url=f.url, cause=f.cause) for f in result.failed]
        return cls(added=result.added, existing=result.existing, failed=failed)


class PollSummaryOut(BaseModel):
    polled: int
    new_items: int
    failed: int

    @classmethod
    def of(cls, summary: PollSummary) -> PollSummaryOut:
        return cls(polled=summary.polled, new_items=summary.new_items, failed=summary.failed)


class PostPatchIn(BaseModel):
    read: bool


class ReadAboveIn(BaseModel):
    post_id: int = Field(description="The selected post: it and every post above it are marked")
    view: View
    feed: int | None = None
    folder: int | None = None
    as_of: str = Field(
        max_length=TIMESTAMP_MAX,
        description="When the client loaded the list (ISO 8601 with offset): posts first seen "
        "later were never on screen and stay unread",
    )


class PostOut(BaseModel):
    id: int
    feed_id: int
    feed_title: str
    url: str
    title: str
    author: str | None
    published_at: datetime | None
    first_seen_at: datetime
    snippet: str
    content_html: str | None
    discussion_url: str | None
    is_summary: bool
    read: bool
    kept_page_id: str | None
    archive_state: str | None
    archive_error: str | None
    has_full_article: bool

    @classmethod
    def of(cls, post: PostView) -> PostOut:
        return cls(
            id=post.id,
            feed_id=post.feed_id,
            feed_title=post.feed_title,
            url=post.url,
            title=post.title,
            author=post.author,
            published_at=post.published_at,
            first_seen_at=post.first_seen_at,
            snippet=post.snippet,
            content_html=post.content_html,
            discussion_url=post.discussion_url,
            is_summary=post.is_summary,
            read=post.read,
            kept_page_id=post.kept_page_id,
            archive_state=post.archive_state,
            archive_error=post.archive_error,
            has_full_article=post.has_full_article,
        )


class PostPageOut(BaseModel):
    items: list[PostOut]
    next_before: str | None = Field(description="Pass as ``before`` for the next page")


class MarkedOut(BaseModel):
    marked: int


class MarkReadIn(BaseModel):
    feed: int | None = None
    folder: int | None = None
    as_of: str = Field(
        max_length=TIMESTAMP_MAX,
        description="When the client loaded the list (ISO 8601 with offset): posts first seen "
        "later were never on screen and stay unread",
    )


class MarkedIdsOut(BaseModel):
    marked: int
    ids: list[int]


class ReadStateIn(BaseModel):
    ids: list[int] = Field(max_length=MAX_READ_IDS)
    read: bool


class ArticleOut(BaseModel):
    markdown: str


class UnreadOut(BaseModel):
    id: int
    unread: int


class ViewCountsOut(BaseModel):
    unread: int
    kept: int
    all: int


class CountsOut(BaseModel):
    feeds: list[UnreadOut]
    folders: list[UnreadOut]
    views: ViewCountsOut

    @classmethod
    def of(cls, counts: Counts) -> CountsOut:
        return cls(
            feeds=[UnreadOut(id=k, unread=v) for k, v in counts.feeds.items()],
            folders=[UnreadOut(id=k, unread=v) for k, v in counts.folders.items()],
            views=ViewCountsOut(unread=counts.unread, kept=counts.kept, all=counts.all),
        )


class SearchPostsIn(BaseModel):
    q: str = Field(min_length=1, max_length=QUERY_MAX, description="Text in a headline or snippet")


class GetPostIn(BaseModel):
    id: int = Field(description="A post id from rss_search_posts")


class TakeawayIn(BaseModel):
    text: str = Field(
        min_length=1, max_length=TAKEAWAY_MAX, description="The takeaway as the author left it"
    )
    session: str = Field(
        min_length=1, max_length=SESSION_ID_MAX, description="The Engage session it came from"
    )


class KeepIn(BaseModel):
    note: str = Field(default="", max_length=NOTE_MAX)
    tags: list[Annotated[str, Field(min_length=1, max_length=TAG_MAX)]] = Field(
        default_factory=list,
        max_length=TAGS_MAX,
        description="Tags for the page; one not in the catalog yet is added to it",
    )
    takeaway: TakeawayIn | None = Field(
        default=None, description="From Engage: written to the page's AI section"
    )


class KeepOut(BaseModel):
    page_id: str
    path: str
    created: bool = Field(description="False when the source's page existed and was updated")
    post: PostOut


class EngageIn(BaseModel):
    runner: str = Field(min_length=1, max_length=RUNNER_MAX)


class SubjectOut(BaseModel):
    kind: str
    ref: str
    title: str


class SessionOut(BaseModel):
    id: str
    runner: str
    status: str
    model: str | None
    read_only: bool
    subject: SubjectOut

    @classmethod
    def of(cls, session: Session) -> SessionOut:
        subject = SubjectOut(
            kind=session.subject.kind, ref=session.subject.ref, title=session.subject.title
        )
        return cls(
            id=session.id,
            runner=session.runner,
            status=session.status,
            model=session.model,
            read_only=session.read_only,
            subject=subject,
        )


class TakeawayOut(BaseModel):
    text: str
    agent: str
    model: str
    session: str

    @classmethod
    def of(cls, takeaway: Takeaway) -> TakeawayOut:
        return cls(
            text=takeaway.text,
            agent=takeaway.agent,
            model=takeaway.model,
            session=takeaway.session,
        )


class SettingsBody(BaseModel):
    retention: int = Field(
        ge=RETENTION_DAYS_MIN,
        le=RETENTION_DAYS_MAX,
        description="Days an unkept post is kept after publication",
    )
    source_folder: str = Field(
        max_length=SOURCE_FOLDER_MAX, description="The KB folder kept posts are written to"
    )

    @classmethod
    def of(cls, settings: RssSettings) -> SettingsBody:
        return cls(retention=settings.retention_days, source_folder=settings.source_folder)


class RssErrorOut(BaseModel):
    error: str
    detail: str
    near: list[str] | None = Field(
        default=None, description="For NearDuplicate: the existing tags it is close to"
    )


_STATUS: dict[type[RssError], int] = {
    FeedNotFound: status.HTTP_404_NOT_FOUND,
    FolderNotFound: status.HTTP_404_NOT_FOUND,
    PostNotFound: status.HTTP_404_NOT_FOUND,
    EngageSessionNotFound: status.HTTP_404_NOT_FOUND,
    FeedExists: status.HTTP_409_CONFLICT,
    PostNotKept: status.HTTP_409_CONFLICT,
    PostOutOfScope: status.HTTP_409_CONFLICT,
    ArchiveNotFailed: status.HTTP_409_CONFLICT,
    EngageBusy: status.HTTP_409_CONFLICT,
    NotAnEngageSession: status.HTTP_400_BAD_REQUEST,
    NotThisPostsSession: status.HTTP_400_BAD_REQUEST,
    FolderExists: status.HTTP_409_CONFLICT,
    # The remote feed failed, not this server: the cause is in ``detail``.
    FeedUnavailable: status.HTTP_502_BAD_GATEWAY,
    ArticleUnavailable: status.HTTP_502_BAD_GATEWAY,
    TakeawayUnavailable: status.HTTP_502_BAD_GATEWAY,
}
# KB errors from Keep. Unavailable is the KB banner's cause; the action was refused whole.
_KB_STATUS: dict[type[KbError], int] = {
    KbUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    InvalidTag: status.HTTP_400_BAD_REQUEST,
    InvalidAssetName: status.HTTP_400_BAD_REQUEST,
    PathOutsideKb: status.HTTP_400_BAD_REQUEST,
    ReservedPath: status.HTTP_400_BAD_REQUEST,
    PageNotFound: status.HTTP_404_NOT_FOUND,
}

# ``Any`` is FastAPI's own type for ``responses`` (free-form OpenAPI fragments).
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": RssErrorOut} for code in (400, 404, 409, 502, 503)
}


def _error(exc: RssError | KbError) -> JSONResponse:
    near = exc.near if isinstance(exc, NearDuplicate) else None
    body = RssErrorOut(error=type(exc).__name__, detail=str(exc), near=near)
    if isinstance(exc, RssError):
        code = _STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST)
    else:  # a tag exists, a stale write: the KB's state conflicts with the request
        code = _KB_STATUS.get(type(exc), status.HTTP_409_CONFLICT)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=code)


async def _guarded(work: Callable[[], Awaitable[Response]]) -> Response:
    """``work``'s response, or its typed RSS or KB error as structured JSON."""
    try:
        return await work()
    except (RssError, KbError) as exc:
        return _error(exc)


async def _respond(
    work: Callable[[], Awaitable[BaseModel | None]], ok: int = status.HTTP_200_OK
) -> Response:
    """``work``'s model as a concrete ``Response`` (so idempotency can store it), or the
    typed error as structured JSON."""

    async def run() -> Response:
        result = await work()
        if result is None:
            return Response(status_code=ok)
        return JSONResponse(jsonable_encoder(result), status_code=ok)

    return await _guarded(run)


def build_router(host: RssHost) -> APIRouter:
    r = APIRouter(prefix="/api/rss", tags=["rss"], responses=_ERRORS)
    keyed = Depends(idempotent(lambda: host.idempotency))

    @r.get("/feeds", response_model=list[FeedOut])
    async def list_feeds() -> list[FeedOut]:
        return [FeedOut.of(v) for v in host.service.list()]

    @r.post("/feeds", status_code=status.HTTP_201_CREATED, response_model=FeedOut)
    async def add_feed(body: FeedIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> FeedOut:
            feed = await host.service.add(body.url, body.folder)
            return FeedOut.of(host.service.view(feed.id))

        return await idem.run(lambda: _respond(run, status.HTTP_201_CREATED))

    @r.delete("/feeds/{feed_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    async def remove_feed(feed_id: int, idem: IdempotentCall = keyed) -> Response:
        async def run() -> None:
            host.service.remove(feed_id)

        return await idem.run(lambda: _respond(run, status.HTTP_204_NO_CONTENT))

    @r.patch("/feeds/{feed_id}", response_model=FeedOut)
    async def move_feed(feed_id: int, body: FeedPatchIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> FeedOut:
            host.service.move(feed_id, body.folder)
            return FeedOut.of(host.service.view(feed_id))

        return await idem.run(lambda: _respond(run))

    @r.put("/feeds/{feed_id}/retention", response_model=FeedOut)
    async def set_retention(
        feed_id: int, body: RetentionIn, idem: IdempotentCall = keyed
    ) -> Response:
        async def run() -> FeedOut:
            retention = body.retention
            checked = (
                None if retention is None else validate_retention(retention.unit, retention.value)
            )
            return FeedOut.of(host.service.set_retention(feed_id, checked))

        return await idem.run(lambda: _respond(run))

    @r.patch("/folders/{folder_id}", response_model=FolderOut)
    async def rename_folder(
        folder_id: int, body: FolderPatchIn, idem: IdempotentCall = keyed
    ) -> Response:
        async def run() -> FolderOut:
            return FolderOut.of(host.service.rename_folder(folder_id, body.name))

        return await idem.run(lambda: _respond(run))

    @r.post("/opml", response_model=ImportResultOut)
    async def import_opml(body: OpmlIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> ImportResultOut:
            return ImportResultOut.of(await host.service.import_opml(body.opml))

        return await idem.run(lambda: _respond(run))

    @r.get(
        "/opml",
        response_class=Response,
        responses={200: {"content": {OPML_MEDIA_TYPE: {}}, "description": "The OPML file"}},
    )
    async def export_opml() -> Response:
        return Response(
            host.service.export_opml(),
            media_type=OPML_MEDIA_TYPE,
            headers={"Content-Disposition": 'attachment; filename="jesinia-feeds.opml"'},
        )

    @r.post("/refresh", response_model=PollSummaryOut)
    async def refresh(idem: IdempotentCall = keyed) -> Response:
        """Poll every feed now; a failing feed is reported on the feed, not here."""

        async def run() -> PollSummaryOut:
            return PollSummaryOut.of(await host.poller.poll_all())

        return await idem.run(lambda: _respond(run))

    @r.post("/feeds/{feed_id}/refresh", response_model=FeedOut)
    async def refresh_feed(feed_id: int, idem: IdempotentCall = keyed) -> Response:
        """Poll one feed now (Retry on a failing feed): the feed as it stands afterwards, with
        its ``warning`` if it still fails."""

        async def run() -> FeedOut:
            await host.poller.poll_feed(feed_id)
            return FeedOut.of(host.service.view(feed_id))

        return await idem.run(lambda: _respond(run))

    @r.get("/posts", response_model=PostPageOut)
    async def list_posts(
        view: View = "unread",
        feed: int | None = None,
        folder: int | None = None,
        before: str | None = Query(default=None, max_length=CURSOR_MAX),
        limit: int = Query(default=PAGE_SIZE, ge=1, le=PAGE_SIZE_MAX),
    ) -> Response:
        """Newest first; ``before`` is the previous page's ``next_before``."""

        async def run() -> PostPageOut:
            cursor = parse_cursor(before) if before is not None else None
            page = host.posts.list(view, feed, folder, cursor, limit)
            next_before = format_cursor(page.next_before) if page.next_before else None
            return PostPageOut(items=[PostOut.of(p) for p in page.items], next_before=next_before)

        return await _respond(run)

    @r.get("/posts/search", response_model=list[PostOut])
    async def search_posts(
        q: str = Query(min_length=1, max_length=QUERY_MAX),
    ) -> list[PostOut]:
        """Headlines and snippets of retained posts (kept ones are searchable in the KB)."""
        return [PostOut.of(p) for p in host.posts.search(q)]

    @r.patch("/posts/{post_id}", response_model=PostOut)
    async def patch_post(post_id: int, body: PostPatchIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> PostOut:
            host.posts.set_read([post_id], body.read)
            return PostOut.of(host.posts.get(post_id))

        return await idem.run(lambda: _respond(run))

    @r.post("/posts/read-above", response_model=MarkedOut)
    async def read_above(body: ReadAboveIn, idem: IdempotentCall = keyed) -> Response:
        async def run() -> MarkedOut:
            as_of = parse_timestamp(body.as_of)
            marked = host.posts.mark_read_above(
                body.post_id, body.view, body.feed, body.folder, as_of
            )
            return MarkedOut(marked=marked)

        return await idem.run(lambda: _respond(run))

    @r.post("/posts/mark-read", response_model=MarkedIdsOut)
    async def mark_read(body: MarkReadIn, idem: IdempotentCall = keyed) -> Response:
        """Mark a feed, a folder or (both null) everything read; the ids come back for Undo."""

        async def run() -> MarkedIdsOut:
            as_of = parse_timestamp(body.as_of)
            ids = host.posts.mark_all_read(body.feed, body.folder, as_of)
            return MarkedIdsOut(marked=len(ids), ids=ids)

        return await idem.run(lambda: _respond(run))

    @r.post("/posts/read-state", response_model=MarkedOut)
    async def read_state(body: ReadStateIn, idem: IdempotentCall = keyed) -> Response:
        """Set the read state of many posts at once (Undo of a mark-read)."""

        async def run() -> MarkedOut:
            return MarkedOut(marked=host.posts.set_read(body.ids, body.read))

        return await idem.run(lambda: _respond(run))

    @r.get("/posts/{post_id}/article", response_model=ArticleOut)
    async def full_article(post_id: int) -> Response:
        """The original article as Markdown, extracted on first request and cached."""

        async def run() -> ArticleOut:
            return ArticleOut(markdown=await host.posts.full_article(post_id))

        return await _respond(run)

    @r.get("/counts", response_model=CountsOut)
    async def counts() -> CountsOut:
        return CountsOut.of(host.posts.counts())

    @r.post(
        "/posts/{post_id}/keep",
        status_code=status.HTTP_201_CREATED,
        response_model=KeepOut,
        responses={200: {"model": KeepOut, "description": "The source's page was updated"}},
    )
    async def keep(post_id: int, body: KeepIn, idem: IdempotentCall = keyed) -> Response:
        """Write the post's KB page now (201), or update the page for the same source (200);
        the archive then runs in the background (``post.archive_state``). Refused whole,
        with the cause, while the KB is unavailable (503)."""

        async def run() -> Response:
            takeaway = None
            if body.takeaway is not None:
                given = body.takeaway
                takeaway = host.engage.resolve(post_id, given.session, given.text)
            kept = await host.keeper.keep(post_id, body.note, body.tags, takeaway)
            out = KeepOut(
                page_id=kept.page_id,
                path=kept.path.as_posix(),
                created=kept.created,
                post=PostOut.of(host.posts.get(post_id)),
            )
            code = status.HTTP_201_CREATED if kept.created else status.HTTP_200_OK
            return JSONResponse(jsonable_encoder(out), status_code=code)

        return await idem.run(lambda: _guarded(run))

    @r.post(
        "/posts/{post_id}/archive", status_code=status.HTTP_202_ACCEPTED, response_model=PostOut
    )
    async def retry_archive(post_id: int, idem: IdempotentCall = keyed) -> Response:
        """Archive a kept post's page again: Retry after "Archive incomplete" only (409
        ``ArchiveNotFailed`` while it runs or once it is done)."""

        async def run() -> PostOut:
            return PostOut.of(host.keeper.retry(post_id))

        return await idem.run(lambda: _respond(run, status.HTTP_202_ACCEPTED))

    @r.post(
        "/posts/{post_id}/engage", status_code=status.HTTP_201_CREATED, response_model=SessionOut
    )
    async def engage(post_id: int, body: EngageIn, idem: IdempotentCall = keyed) -> Response:
        """A read-only agent session about the post; chat in it through ``/api/agents``."""

        async def run() -> SessionOut:
            return SessionOut.of(await host.engage.start(post_id, body.runner))

        return await idem.run(lambda: _respond(run, status.HTTP_201_CREATED))

    @r.post("/sessions/{session_id}/takeaway", response_model=TakeawayOut)
    async def takeaway(session_id: str, idem: IdempotentCall = keyed) -> Response:
        """Ask the engaged agent for a 1–3 sentence takeaway, for the author to edit and
        send with Keep; nothing is written."""

        async def run() -> TakeawayOut:
            return TakeawayOut.of(await host.engage.takeaway(session_id))

        return await idem.run(lambda: _respond(run))

    @r.get("/reader", response_model=ReaderSetupOut)
    async def reader_setup() -> ReaderSetupOut:
        return ReaderSetupOut(
            has_password=host.reader.has_password(), server_url=host.reader_server_url()
        )

    @r.post("/reader/password", response_model=ReaderPasswordOut)
    async def new_reader_password() -> ReaderPasswordOut:
        """A new reader password for NetNewsWire; the old one and its sign-ins stop working.

        Deliberately not idempotent: a stored response would keep the password in clear
        in ``rss.sqlite``, and a retry that makes another password only revokes the first
        (the user copies whichever one they were shown)."""
        password = host.reader.create_password()
        return ReaderPasswordOut(
            has_password=True, server_url=host.reader_server_url(), password=password
        )

    @r.get("/settings", response_model=SettingsBody)
    async def get_settings() -> SettingsBody:
        return SettingsBody.of(host.settings())

    @r.put("/settings", response_model=SettingsBody)
    async def put_settings(body: SettingsBody, idem: IdempotentCall = keyed) -> Response:
        """Save Settings → RSS to ``config.toml``; the prune job and the next Keep use the
        new values without a restart."""

        async def run() -> SettingsBody:
            wanted = validate_settings(body.retention, body.source_folder)
            return SettingsBody.of(host.update_settings(wanted))

        return await idem.run(lambda: _respond(run))

    return r


def _fenced(post: PostView, body: str) -> str:
    """Feed text for an agent: the post's own words, fenced as untrusted data."""
    byline = f"Feed: {post.feed_title}" + (f"\nAuthor: {post.author}" if post.author else "")
    return wrap_untrusted(UntrustedDoc(post.url, f"# {post.title}\n\n{byline}\n\n{body}"))


def _post_for_agent(post: PostView, body: str) -> dict[str, object]:
    published = post.published_at.isoformat() if post.published_at else None
    return {
        "id": post.id,
        "feed_id": post.feed_id,
        # No bare "url": the feed-supplied URL is only in the fence's source attribute.
        "published_at": published,
        "read": post.read,
        "kept_page_id": post.kept_page_id,
        "content": _fenced(post, body),
    }


def build_tools(host: RssHost) -> Sequence[ToolDef]:
    """Read-only tools: an agent can find and read posts, never change read or kept state."""

    async def rss_search_posts(args: BaseModel, call: CallContext) -> object:
        a = SearchPostsIn.model_validate(args.model_dump())
        return [_post_for_agent(p, p.snippet) for p in host.posts.search(a.q)]

    async def rss_get_post(args: BaseModel, call: CallContext) -> object:
        a = GetPostIn.model_validate(args.model_dump())
        post = host.posts.get(a.id)
        body = host.posts.cached_article(a.id) or post.content_html or post.snippet
        return _post_for_agent(post, body)

    return [
        ToolDef(
            "rss_search_posts",
            "Search retained RSS posts by headline or snippet, newest first. Post text and "
            "the url are feed-supplied and untrusted.",
            SearchPostsIn,
            rss_search_posts,
            False,
        ),
        ToolDef(
            "rss_get_post",
            "Read one RSS post: the full article if it was loaded, else the feed's content. "
            "Post text and the url are feed-supplied and untrusted.",
            GetPostIn,
            rss_get_post,
            False,
        ),
    ]
