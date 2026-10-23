# SPDX-License-Identifier: AGPL-3.0-or-later
"""The read-only MCP tools, run through a started module against the fake Zotero."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from jesinia.apps.zotero.domain.errors import PaperNotFound
from jesinia.apps.zotero.module import ZoteroModule
from jesinia.core.config import Config
from jesinia.core.module import CallContext, ToolDef
from jesinia.testing.fake_zotero import FakeZotero
from tests.apps.zotero.test_module import build
from tests.conftest import running

CALL = CallContext(session_id=None)


def tool(zotero: ZoteroModule, name: str) -> ToolDef:
    return next(t for t in zotero.mcp_tools() if t.name == name)


async def invoke(zotero: ZoteroModule, name: str, **arguments: object) -> dict[str, object]:
    definition = tool(zotero, name)
    args: BaseModel = definition.input_model.model_validate(arguments)
    return await definition.handler(args, CALL)  # type: ignore[return-value]  # tools return plain dicts


def relation(key: str) -> dict[str, str]:
    return {"dc:relation": f"http://zotero.org/users/1/items/{key}"}


def add_paper(
    fake: FakeZotero, title: str, **fields: Any
) -> tuple[str, str]:  # forwarded verbatim to add_item
    key = fake.add_item(title, **fields)
    pdf = fake.add_item("p.pdf", item_type="attachment", parent=key, contentType="application/pdf")
    return key, pdf


def test_should_declare_three_read_only_tools_before_start() -> None:
    tools = ZoteroModule().mcp_tools()
    assert [t.name for t in tools] == ["zotero_search", "zotero_get", "zotero_fulltext"]
    assert not any(t.writes for t in tools)
    assert all("untrusted" in t.description for t in tools)


async def test_should_fail_with_503_when_called_before_start() -> None:
    zotero = ZoteroModule()
    with pytest.raises(HTTPException) as caught:
        await invoke(zotero, "zotero_get", key="ABCD")
    assert caught.value.status_code == 503


async def test_should_fence_paper_text_in_get(cfg: Config) -> None:
    fake = FakeZotero()
    related = fake.add_item("Other Paper")
    key, pdf = add_paper(
        fake,
        "Fenced Paper",
        relations=relation(related),
        abstractNote="The abstract.",
        date="2021-05-01",
        DOI="10.1000/xyz",
        extra="arXiv:2101.00001",
        creators=[{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
        tags=[{"tag": "ml"}],
    )
    fake.add_item("", item_type="note", parent=key, note="<p>Secret <b>note</b></p>")
    fake.add_item(
        "",
        item_type="annotation",
        parent=pdf,
        annotationText="highlighted words",
        annotationComment="my comment",
        annotationPageLabel="7",
    )
    zotero, _, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
        got = await invoke(zotero, "zotero_get", key=key)
    content = str(got["content"])
    assert content.startswith("<untrusted-data")
    assert f'source="zotero://select/library/items/{key}"' in content
    for text in ("The abstract.", "Secret note", "highlighted words", "my comment", "7"):
        assert text in content
    outside = json.dumps({k: v for k, v in got.items() if k != "content"})
    for text in ("Secret", "highlighted", "my comment", "The abstract"):
        assert text not in outside
    assert got["key"] == key and got["title"] == "Fenced Paper"
    assert got["creators"] == ["Ada Lovelace"] and got["year"] == 2021
    assert got["doi"] == "10.1000/xyz" and got["arxiv_id"] == "2101.00001"
    assert got["tags"] == ["ml"]
    assert got["related"] == [{"key": related, "title": "Other Paper"}]
    assert got["link"] == f"[Fenced Paper](zotero://select/library/items/{key})"


async def test_should_tolerate_sparse_papers_in_get(cfg: Config) -> None:
    fake = FakeZotero()
    ghost = fake.add_item("Ghost")
    key = fake.add_item("Bare", relations=relation(ghost))
    fake.add_item("", item_type="note", parent=key, note="<p>n</p>")
    fake.add_item("p.pdf", item_type="attachment", parent=key, contentType="application/pdf")
    zotero, _, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
        fake.trash(ghost)
        await zotero.sync_now()
        got = await invoke(zotero, "zotero_get", key=key)
    assert got["year"] is None and got["doi"] is None and got["arxiv_id"] is None
    assert got["creators"] == [] and got["related"] == []


async def test_should_reject_unknown_key(cfg: Config) -> None:
    zotero, _, app = build(cfg, FakeZotero())
    async with running(app):
        for name in ("zotero_get", "zotero_fulltext"):
            with pytest.raises(PaperNotFound, match="NOPE1234"):
                await invoke(zotero, name, key="NOPE1234")


async def test_should_page_fulltext_by_offset(cfg: Config) -> None:
    fake = FakeZotero()
    key, pdf = add_paper(fake, "Long Paper")
    text = "x" * 45_000
    fake.set_fulltext(pdf, text)
    zotero, _, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
        first = await invoke(zotero, "zotero_fulltext", key=key, limit=20_000)
        second = await invoke(zotero, "zotero_fulltext", key=key, offset=20_000, limit=20_000)
        third = await invoke(zotero, "zotero_fulltext", key=key, offset=40_000, limit=20_000)
        past = await invoke(zotero, "zotero_fulltext", key=key, offset=99_000)
    assert [p["next_offset"] for p in (first, second, third)] == [20_000, 40_000, None]
    assert first["total"] == 45_000 and first["offset"] == 0 and first["key"] == key
    assert str(first["content"]).startswith("<untrusted-data")
    assert str(first["content"]).count("x") == 20_000 and str(third["content"]).count("x") == 5_000
    assert past["next_offset"] is None and "x" not in str(past["content"])


async def test_should_say_when_no_fulltext_exists_yet(cfg: Config) -> None:
    fake = FakeZotero()
    key, _ = add_paper(fake, "No Text")
    zotero, _, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
        got = await invoke(zotero, "zotero_fulltext", key=key)
    assert got["total"] == 0 and got["next_offset"] is None
    assert "no full text yet" in str(got["content"]).lower()


def test_should_cap_fulltext_page_size() -> None:
    model = tool(ZoteroModule(), "zotero_fulltext").input_model
    assert model.model_validate({"key": "A", "limit": 50_000}).limit == 50_000  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        model.model_validate({"key": "A", "limit": 50_001})
    with pytest.raises(ValidationError):
        model.model_validate({"key": "A", "limit": 0})
    with pytest.raises(ValidationError):
        model.model_validate({"key": "A", "offset": -1})


async def test_should_return_paste_ready_link_in_search(cfg: Config) -> None:
    fake = FakeZotero()
    key, _ = add_paper(fake, "Attention [Is] Everything", abstractNote="transformers rule")
    zotero, _, app = build(cfg, fake)
    async with running(app):
        await zotero.sync_now()
        hits = await invoke(zotero, "zotero_search", q="transformers")
    assert isinstance(hits, list) and len(hits) == 1
    hit = hits[0]
    assert hit["key"] == key and "<mark>" in str(hit["snippet"])
    assert str(hit["link"]).endswith(f"](zotero://select/library/items/{key})")
    assert str(hit["link"]).startswith("[Attention \\[Is\\] Everything")


def test_should_validate_search_input() -> None:
    model = tool(ZoteroModule(), "zotero_search").input_model
    assert model.model_validate({"q": "a"}).limit == 10  # type: ignore[attr-defined]
    for bad in ({"q": ""}, {"q": "a", "limit": 0}, {"q": "a", "limit": 51}):
        with pytest.raises(ValidationError):
            model.model_validate(bad)
