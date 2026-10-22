# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request/response models shared by the HTTP routes and MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jesinia.services.kb.application.ports import Conflict
from jesinia.services.kb.application.service import MoveResult, PageWithRev, TreeNode
from jesinia.services.kb.application.tags import (
    RenamePreview,
    RenameResult,
    Suggestion,
    TagCheck,
    TagInfo,
)


class PageOut(BaseModel):
    id: str
    title: str
    path: str
    body: str
    jesinia: dict[str, object]
    foreign: dict[str, object]
    rev: str

    @classmethod
    def of(cls, p: PageWithRev) -> PageOut:
        pg = p.page
        return cls(
            id=pg.id,
            title=pg.title,
            path=str(pg.path),
            body=pg.body,
            jesinia=dict(pg.jesinia),
            foreign=dict(pg.foreign),
            rev=p.rev,
        )


class PageIn(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1)
    folder: str = ""
    body: str = ""
    jesinia: dict[str, object] = Field(default_factory=dict)
    expected_rev: str | None = None


class BodyIn(BaseModel):
    body: str
    expected_rev: str


class MoveIn(BaseModel):
    folder: str
    title: str | None = None


class MoveOut(BaseModel):
    page: PageOut
    relinked: list[str]
    skipped: list[str]

    @classmethod
    def of(cls, r: MoveResult) -> MoveOut:
        return cls(page=PageOut.of(r.page), relinked=list(r.relinked), skipped=list(r.skipped))


class TreeOut(BaseModel):
    path: str
    kind: Literal["folder", "page"]
    title: str | None = None
    id: str | None = None
    pages: int
    children: list[TreeOut] = Field(default_factory=list)

    @classmethod
    def of(cls, n: TreeNode) -> TreeOut:
        return cls(
            path=str(n.path),
            kind=n.kind,
            title=n.title,
            id=n.id,
            pages=n.pages,
            children=[cls.of(c) for c in n.children],
        )


class ConflictOut(BaseModel):
    kind: str
    id: str
    paths: list[str]
    detected_at: str

    @classmethod
    def of(cls, c: Conflict) -> ConflictOut:
        return cls(kind=c.kind, id=c.id, paths=[str(p) for p in c.paths], detected_at=c.detected_at)


class PathIn(BaseModel):
    path: str


class IdIn(BaseModel):
    id: str


class EmptyIn(BaseModel):
    pass


class AiAuthorIn(BaseModel):
    agent: str = Field(min_length=1, max_length=80, description="e.g. claude-code, codex")
    model: str = Field(min_length=1, max_length=80, description="The model that wrote the text")


class WriteToolIn(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1)
    # None (omitted) keeps an existing page where it is; a new page goes to the KB root.
    folder: str | None = Field(
        None, description="Folder to write in; omit on update to keep the page where it is"
    )
    body: str = ""
    # None (omitted) keeps an existing page's metadata on update; {} clears it.
    jesinia: dict[str, object] | None = Field(
        None, description="Front-matter `jesinia:` metadata; omit on update to keep it"
    )
    expected_rev: str | None = None
    provenance: AiAuthorIn | None = Field(
        None,
        description="New pages only: you wrote the body, so Jesinia records it inside an "
        "AI provenance block (time and session stamped by the server)",
    )


class MoveToolIn(BaseModel):
    id: str
    folder: str
    title: str | None = None


# -- tags ------------------------------------------------------------------------------
TAG_MAX = 100


class TagInfoOut(BaseModel):
    tag: str
    meaning: str
    aliases: list[str]
    retired: bool
    count: int

    @classmethod
    def of(cls, t: TagInfo) -> TagInfoOut:
        return cls(
            tag=t.tag, meaning=t.meaning, aliases=list(t.aliases), retired=t.retired, count=t.count
        )


class TagCheckOut(BaseModel):
    normalised: str
    exists: bool
    canonical: str | None
    near: list[str]
    same_meaning: str | None

    @classmethod
    def of(cls, c: TagCheck) -> TagCheckOut:
        return cls(
            normalised=c.normalised,
            exists=c.exists,
            canonical=c.canonical,
            near=list(c.near),
            same_meaning=c.same_meaning,
        )


class SuggestIn(BaseModel):
    text: str = Field(max_length=20_000, description="The text to tag (title, note, excerpt)")
    limit: int = Field(5, ge=1, le=20)


class SuggestionOut(BaseModel):
    tag: str
    new: bool = Field(description="Not in the catalog yet; accepting it creates it")

    @classmethod
    def of(cls, s: Suggestion) -> SuggestionOut:
        return cls(tag=s.tag, new=s.new)


class TagNameIn(BaseModel):
    tag: str = Field(min_length=1, max_length=TAG_MAX)


class TagIn(TagNameIn):
    meaning: str = Field("", max_length=500)
    force: bool = Field(False, description="Add even though near-matches exist (user override)")


class UncatalogedOut(BaseModel):
    tag: str
    count: int


class RenamePreviewIn(BaseModel):
    old: str = Field(min_length=1, max_length=TAG_MAX)
    new: str = Field(min_length=1, max_length=TAG_MAX)


class RenameIn(RenamePreviewIn):
    revs: dict[str, str] = Field(description="Page id → rev, from the preview")


class MergeIn(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=50)
    into: str = Field(min_length=1, max_length=TAG_MAX)
    revs: dict[str, str] = Field(description="Page id → rev, from the previews")


class PageRefOut(BaseModel):
    id: str
    title: str
    rev: str


class RenamePreviewOut(BaseModel):
    pages: list[PageRefOut]
    skipped_reserved: list[str]

    @classmethod
    def of(cls, p: RenamePreview) -> RenamePreviewOut:
        return cls(
            pages=[PageRefOut(id=r.id, title=r.title, rev=r.rev) for r in p.pages],
            skipped_reserved=list(p.skipped_reserved),
        )


class RenameResultOut(BaseModel):
    updated: list[str]
    skipped_changed: list[str]

    @classmethod
    def of(cls, r: RenameResult) -> RenameResultOut:
        return cls(updated=list(r.updated), skipped_changed=list(r.skipped_changed))


class TagErrorOut(BaseModel):
    error: str
    detail: str
    near: list[str] | None = None
