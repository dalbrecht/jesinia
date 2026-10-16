# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request/response models shared by the HTTP routes and MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jesinia.services.kb.application.ports import Conflict
from jesinia.services.kb.application.service import MoveResult, PageWithRev, TreeNode


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


class WriteToolIn(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1)
    folder: str = ""
    body: str = ""
    jesinia: dict[str, object] = Field(default_factory=dict)
    expected_rev: str | None = None


class MoveToolIn(BaseModel):
    id: str
    folder: str
    title: str | None = None
