# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local API client, driven against the in-memory fake Zotero over ASGI."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jesinia.apps.zotero.domain.errors import (
    AuthorizeTooSoon,
    StaleVersion,
    WriteTokenUsed,
    ZoteroError,
    ZoteroUnavailable,
)
from jesinia.apps.zotero.domain.identifiers import ItemKey
from jesinia.apps.zotero.infrastructure.local_api import LocalApiClient, parse_paper
from jesinia.testing.fake_zotero import FakeZotero

BASE_URL = "http://127.0.0.1:23119"
TOKEN = "t" * 32


class MemoryKeyStore:
    def __init__(self, key: str | None) -> None:
        self.key = key

    async def read(self) -> str | None:
        return self.key

    async def write(self, key: str) -> None:
        self.key = key


@pytest.fixture
def fake() -> FakeZotero:
    return FakeZotero()


def client(fake: FakeZotero, key: str | None = "k") -> LocalApiClient:
    return LocalApiClient(BASE_URL, MemoryKeyStore(key), transport=fake.transport())


async def test_should_page_changed_items_past_100(fake: FakeZotero) -> None:
    for index in range(130):
        fake.add_item(title=f"t{index}")
    items, version = await client(fake).changed_since(0)
    assert len(items) == 130 and version == fake.version


async def test_should_only_return_items_changed_since_version(fake: FakeZotero) -> None:
    fake.add_item(title="old")
    cutoff = fake.version
    new_key = fake.add_item(title="new")
    items, _ = await client(fake).changed_since(cutoff)
    assert [item["key"] for item in items] == [new_key]


async def test_should_report_library_version(fake: FakeZotero) -> None:
    fake.add_item(title="x")
    assert await client(fake).library_version() == fake.version


async def test_should_list_live_keys_without_trashed_or_removed(fake: FakeZotero) -> None:
    kept, trashed, removed = (fake.add_item(title=name) for name in "abc")
    fake.trash(trashed)
    fake.remove(removed)
    assert await client(fake).live_keys() == {kept}
    assert await client(fake).live_keys() == {ItemKey(kept)}


async def test_should_list_trashed_keys_since_version(fake: FakeZotero) -> None:
    early, late = fake.add_item(title="a"), fake.add_item(title="b")
    fake.trash(early)
    cutoff = fake.version
    fake.trash(late)
    assert await client(fake).trashed_since(0) == [early, late]
    assert await client(fake).trashed_since(cutoff) == [late]


async def test_should_not_expose_deleted_endpoint(fake: FakeZotero) -> None:
    async with httpx.AsyncClient(transport=fake.transport(), base_url=BASE_URL) as http:
        assert (await http.get("/api/users/0/deleted", params={"since": 0})).status_code == 404


async def test_should_get_item_and_children(fake: FakeZotero) -> None:
    parent = fake.add_item(title="paper")
    note = fake.add_item(item_type="note", parent=parent, note="<p>hi</p>")
    fake.add_item(title="other")
    api = client(fake)
    assert (await api.item(parent))["version"] == fake.item_version(parent)
    assert [child["key"] for child in await api.children(parent)] == [note]


async def test_should_raise_when_item_missing(fake: FakeZotero) -> None:
    with pytest.raises(ZoteroError, match="404"):
        await client(fake).item(ItemKey("NOPE0000"))


async def test_should_return_fulltext_or_none(fake: FakeZotero) -> None:
    with_text, without = fake.add_item(title="a"), fake.add_item(title="b")
    fake.set_fulltext(with_text, "hello world")
    api = client(fake)
    assert await api.fulltext(with_text) == "hello world"
    assert await api.fulltext(without) is None


async def test_should_report_fulltext_versions_since(fake: FakeZotero) -> None:
    first, second = fake.add_item(title="a"), fake.add_item(title="b")
    fake.set_fulltext(first, "one")
    cutoff = fake.fulltext_version
    fake.set_fulltext(second, "two")
    item_version = fake.item_version(second)
    versions = await client(fake).fulltext_versions(cutoff)
    assert versions == {second: fake.fulltext_version}
    assert fake.item_version(second) == item_version
    assert set(await client(fake).fulltext_versions(0)) == {first, second}


async def test_should_create_items_in_batches_and_return_keys_in_order(fake: FakeZotero) -> None:
    payload: list[dict[str, object]] = [{"itemType": "book", "title": f"t{i}"} for i in range(60)]
    keys = await client(fake).create_items(payload)
    assert len(keys) == 60 and len(set(keys)) == 60
    assert fake.item_title(keys[55]) == "t55"
    assert [method for method, _, _ in fake.writes] == ["POST", "POST"]


async def test_should_create_nothing_for_empty_list(fake: FakeZotero) -> None:
    assert await client(fake).create_items([]) == []
    assert fake.writes == []


async def test_should_send_write_token_and_reject_reuse(fake: FakeZotero) -> None:
    api = client(fake)
    await api.create_items([{"itemType": "book", "title": "a"}], write_token=TOKEN)
    assert fake.last_headers["zotero-write-token"] == TOKEN
    with pytest.raises(WriteTokenUsed):
        await api.create_items([{"itemType": "book", "title": "a"}], write_token=TOKEN)


async def test_should_reject_write_token_batch_over_fifty(fake: FakeZotero) -> None:
    payload: list[dict[str, object]] = [{"itemType": "book", "title": "x"}] * 51
    with pytest.raises(ValueError, match="at most 50"):
        await client(fake).create_items(payload, write_token=TOKEN)
    assert fake.writes == []


async def test_fake_should_reject_malformed_write_token(fake: FakeZotero) -> None:
    with pytest.raises(ZoteroError, match="400"):
        await client(fake).create_items([{"itemType": "book"}], write_token="short")


async def test_should_treat_other_412_on_create_as_plain_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(412, text="Library changed")
        return httpx.Response(200, json=[])

    mock = httpx.MockTransport(respond)
    api = LocalApiClient(BASE_URL, MemoryKeyStore("k"), transport=_with_server_id(mock))
    with pytest.raises(ZoteroError, match="Library changed") as raised:
        await api.create_items([{"itemType": "book"}])
    assert not isinstance(raised.value, WriteTokenUsed)


async def test_should_never_put_the_api_key_in_error_text() -> None:
    mock = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    api = LocalApiClient(
        BASE_URL, MemoryKeyStore("secret-key-123"), transport=_with_server_id(mock)
    )
    with pytest.raises(ZoteroError) as raised:
        await api.patch_item(ItemKey("K"), 1, {"title": "y"})
    assert "secret-key-123" not in str(raised.value) and "secret-key-123" not in repr(raised.value)


def _with_server_id(inner: httpx.MockTransport) -> httpx.AsyncBaseTransport:
    class Wrapper(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await inner.handle_async_request(request)
            response.headers["Zotero-Server-ID"] = "S"
            return response

    return Wrapper()


async def test_should_map_other_transport_errors_to_not_running() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    api = LocalApiClient(BASE_URL, MemoryKeyStore("k"), transport=httpx.MockTransport(fail))
    with pytest.raises(ZoteroUnavailable, match="ReadTimeout") as raised:
        await api.library_version()
    assert raised.value.state == "not_running"


async def test_should_restart_paging_when_library_changes_between_pages(fake: FakeZotero) -> None:
    keys = [fake.add_item(title=f"t{i}") for i in range(130)]
    bumps: list[int] = []

    def bump_once(start: int) -> None:
        if not bumps:
            bumps.append(start)
            fake.add_item(title="late")

    fake.on_page = bump_once
    items, version = await client(fake).changed_since(0)
    assert len(items) == 131 and version == fake.version and keys[0] in {i["key"] for i in items}


async def test_should_give_up_when_library_never_stops_changing(fake: FakeZotero) -> None:
    for index in range(101):
        fake.add_item(title=f"t{index}")

    def churn(start: int) -> None:
        fake.add_item(title="churn")

    fake.on_page = churn
    with pytest.raises(ZoteroError, match="kept changing"):
        await client(fake).changed_since(0)


async def test_should_page_without_total_results_header(fake: FakeZotero) -> None:
    for index in range(230):
        fake.add_item(title=f"t{index}")
    fake.omit_total = True
    items, _ = await client(fake).changed_since(0)
    assert len(items) == 230


async def test_should_page_when_total_is_an_exact_multiple_of_page_size(fake: FakeZotero) -> None:
    for index in range(100):
        fake.add_item(title=f"t{index}")
    items, _ = await client(fake).changed_since(0)
    assert len(items) == 100


async def test_should_ignore_proxy_environment(
    fake: FakeZotero, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401 - forwards httpx's open kwargs
        seen.append(kwargs.get("trust_env"))
        return real(**kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://bogus.invalid:1")
    monkeypatch.setenv("ALL_PROXY", "http://bogus.invalid:1")
    monkeypatch.setattr(httpx, "AsyncClient", spy)
    assert await client(fake).status() == "connected"
    assert seen == [False]


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://127.0.0.1:23119", "http://192.168.1.5"]
)
def test_should_reject_non_loopback_base_url(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        LocalApiClient(url, MemoryKeyStore("k"))


@pytest.mark.parametrize("url", ["http://localhost:23119", "http://[::1]:23119"])
def test_should_accept_loopback_base_urls(url: str) -> None:
    LocalApiClient(url, MemoryKeyStore("k"))


async def test_should_raise_when_create_reports_failures(fake: FakeZotero) -> None:
    with pytest.raises(ZoteroError, match="failed"):
        await client(fake).create_items([{"title": "no item type"}])


async def test_should_patch_item_and_return_new_version(fake: FakeZotero) -> None:
    key = fake.add_item(title="x")
    new_version = await client(fake).patch_item(key, fake.item_version(key), {"title": "y"})
    assert new_version == fake.version == fake.item_version(key)
    assert fake.item_title(key) == "y"


async def test_should_raise_stale_version_on_412(fake: FakeZotero) -> None:
    key = fake.add_item(title="x")
    with pytest.raises(StaleVersion):
        await client(fake).patch_item(key, version=0, fields={"title": "y"})


async def test_should_send_api_key_and_server_id_on_writes(fake: FakeZotero) -> None:
    key = fake.add_item(title="x")
    await client(fake).patch_item(key, fake.item_version(key), {"title": "y"})
    assert fake.last_headers["zotero-api-key"] == "k"
    assert fake.last_headers["zotero-server-id"] == fake.server_id
    assert fake.last_headers["if-unmodified-since-version"] == str(fake.version - 1)


async def test_should_record_writes(fake: FakeZotero) -> None:
    key = fake.add_item(title="x")
    await client(fake).patch_item(key, fake.item_version(key), {"title": "y"})
    assert fake.writes == [("PATCH", f"/api/users/0/items/{key}", {"title": "y"})]


async def test_should_report_unauthorized_when_writing_without_a_key(fake: FakeZotero) -> None:
    key = fake.add_item(title="x")
    with pytest.raises(ZoteroUnavailable) as raised:
        await client(fake, key=None).patch_item(key, 1, {"title": "y"})
    assert raised.value.state == "unauthorized"
    assert fake.writes == []


async def test_should_report_unauthorized_when_zotero_rejects_the_key(fake: FakeZotero) -> None:
    fake.valid_key = "other"
    key = fake.add_item(title="x")
    with pytest.raises(ZoteroUnavailable) as raised:
        await client(fake).patch_item(key, fake.item_version(key), {"title": "y"})
    assert raised.value.state == "unauthorized"


async def test_should_fetch_server_id_on_demand_then_cache_it(fake: FakeZotero) -> None:
    api = client(fake)
    original = fake.server_id
    assert await api.server_id() == original
    fake.server_id = "changed"
    assert await api.server_id() == original
    await api.library_version()
    assert await api.server_id() == "changed"


async def test_should_report_connected(fake: FakeZotero) -> None:
    assert await client(fake).status() == "connected"


@pytest.mark.parametrize("fake_mode, state", [("off", "api_off"), ("unauthorized", "unauthorized")])
async def test_should_report_unavailable_state(
    fake: FakeZotero, fake_mode: str, state: str
) -> None:
    fake.mode = fake_mode
    assert await client(fake).status() == state


async def test_should_report_not_running_on_connection_error(fake: FakeZotero) -> None:
    fake.down = True
    assert await client(fake).status() == "not_running"
    with pytest.raises(ZoteroUnavailable) as raised:
        await client(fake).library_version()
    assert raised.value.state == "not_running"


async def test_should_raise_a_typed_error_on_unexpected_status(fake: FakeZotero) -> None:
    fake.mode = "broken"
    with pytest.raises(ZoteroError, match="500"):
        await client(fake).library_version()


async def test_should_surface_unrecognised_forbidden_as_zotero_error(fake: FakeZotero) -> None:
    fake.mode = "forbidden"
    with pytest.raises(ZoteroError, match="403"):
        await client(fake).library_version()


async def test_should_use_a_default_transport_when_none_given() -> None:
    api = LocalApiClient("http://127.0.0.1:1/api", MemoryKeyStore("k"))
    assert await api.status() == "not_running"


async def test_fake_should_require_version_header_and_know_missing_items(fake: FakeZotero) -> None:
    key = fake.add_item(title="x")
    headers = {"Zotero-API-Key": "k"}
    async with httpx.AsyncClient(transport=fake.transport(), base_url=BASE_URL) as http:
        missing_version = await http.patch(f"/api/users/0/items/{key}", json={}, headers=headers)
        unknown = await http.patch("/api/users/0/items/NOPE0000", json={}, headers=headers)
    assert (missing_version.status_code, unknown.status_code) == (428, 404)


async def test_should_fail_loudly_when_zotero_sends_no_server_id() -> None:
    bare = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    api = LocalApiClient(BASE_URL, MemoryKeyStore("k"), transport=bare)
    with pytest.raises(ZoteroError, match="Server-ID"):
        await api.server_id()


def test_authorize_endpoint_is_served_by_the_fake(fake: FakeZotero) -> None:
    import asyncio

    async def call() -> dict[str, object]:
        async with httpx.AsyncClient(transport=fake.transport(), base_url=BASE_URL) as http:
            missing = await http.post("/api/local/authorize", json={"appName": "Jesinia"})
            assert missing.status_code == 428
            response = await http.post(
                "/api/local/authorize",
                json={"appName": "Jesinia"},
                headers={"Zotero-Server-ID": fake.server_id},
            )
            return dict(response.json())

    assert asyncio.run(call()) == {"key": fake.issued_key, "remember": True}


def test_client_has_no_delete_capability() -> None:
    assert not [name for name in dir(LocalApiClient) if "delete" in name.lower()]


def test_should_parse_standalone_pdf_as_standalone_paper() -> None:
    paper = parse_paper(
        {
            "key": "P1",
            "version": 3,
            "data": {
                "itemType": "attachment",
                "contentType": "application/pdf",
                "title": "2510.15061",
                "filename": "2510.15061.pdf",
            },
        },
        [],
    )
    assert paper.standalone and paper.title == "2510.15061"
    assert [attachment.key for attachment in paper.attachments] == ["P1"]
    assert paper.attachments[0].filename == "2510.15061.pdf"


def test_should_parse_standalone_pdf_annotations() -> None:
    paper = parse_paper(
        {
            "key": "P1",
            "version": 1,
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
        },
        [_annotation("A1", "P1")],
    )
    assert [a.key for a in paper.attachments[0].annotations] == ["A1"]


def _annotation(key: str, parent: str) -> dict[str, object]:
    return {
        "key": key,
        "version": 1,
        "data": {
            "itemType": "annotation",
            "parentItem": parent,
            "annotationText": "quoted",
            "annotationComment": "thought",
            "annotationPageLabel": "4",
            "annotationColor": "#ffd400",
        },
    }


def test_should_parse_regular_paper_with_children() -> None:
    raw: dict[str, object] = {
        "key": "R1",
        "version": 9,
        "data": {
            "itemType": "journalArticle",
            "title": "A Paper",
            "creators": [
                {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                {"creatorType": "editor", "name": "Acme Group"},
            ],
            "date": "2025-10-01",
            "abstractNote": "About things.",
            "DOI": "10.1000/x",
            "url": "https://arxiv.org/abs/2510.15061",
            "extra": "arXiv: 2510.15061",
            "tags": [{"tag": "ml"}, {"tag": "auto", "type": 1}],
            "relations": {
                "dc:relation": [
                    "http://zotero.org/users/local/abc/items/RELA0001",
                    "http://zotero.org/users/12345/items/RELB0002",
                ]
            },
        },
    }
    children: list[dict[str, object]] = [
        {"key": "N1", "version": 1, "data": {"itemType": "note", "note": "<p>n</p>"}},
        {
            "key": "F1",
            "version": 1,
            "data": {
                "itemType": "attachment",
                "contentType": "application/pdf",
                "title": "Full Text PDF",
                "filename": "p.pdf",
                "url": "https://example.org/p.pdf",
                "parentItem": "R1",
            },
        },
        {
            "key": "S1",
            "version": 1,
            "data": {"itemType": "attachment", "contentType": "text/html", "parentItem": "R1"},
        },
        _annotation("A1", "F1"),
        _annotation("A2", "OTHER"),
    ]
    paper = parse_paper(raw, children)
    assert not paper.standalone
    assert (paper.key, paper.version, paper.item_type, paper.title) == (
        "R1",
        9,
        "journalArticle",
        "A Paper",
    )
    assert [(c.first, c.last, c.kind) for c in paper.creators] == [
        ("Ada", "Lovelace", "author"),
        ("", "Acme Group", "editor"),
    ]
    assert (paper.date, paper.abstract, paper.doi, paper.extra) == (
        "2025-10-01",
        "About things.",
        "10.1000/x",
        "arXiv: 2510.15061",
    )
    assert [(t.name, t.automatic) for t in paper.tags] == [("ml", False), ("auto", True)]
    assert paper.related == ("RELA0001", "RELB0002")
    assert [(n.key, n.html) for n in paper.notes] == [("N1", "<p>n</p>")]
    assert [a.key for a in paper.attachments] == ["F1"]
    attachment = paper.attachments[0]
    assert (attachment.title, attachment.url, attachment.content_type) == (
        "Full Text PDF",
        "https://example.org/p.pdf",
        "application/pdf",
    )
    assert [(a.key, a.text, a.comment, a.page_label, a.color) for a in attachment.annotations] == [
        ("A1", "quoted", "thought", "4", "#ffd400")
    ]


def test_should_parse_sparse_item_and_single_relation() -> None:
    paper = parse_paper(
        {
            "key": "R2",
            "version": 1,
            "data": {
                "itemType": "book",
                "relations": {"dc:relation": "http://zotero.org/users/local/a/items/ONLY0001"},
            },
        },
        [],
    )
    assert (paper.title, paper.creators, paper.tags, paper.doi) == ("", (), (), "")
    assert paper.related == ("ONLY0001",)
    assert parse_paper({"key": "R3", "version": 1, "data": {"itemType": "book"}}, []).related == ()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def authorizing_client(fake: FakeZotero, store: MemoryKeyStore, clock: Clock) -> LocalApiClient:
    return LocalApiClient(BASE_URL, store, transport=fake.transport(), clock=clock)


async def test_should_store_key_when_always_allow_is_chosen(fake: FakeZotero) -> None:
    store = MemoryKeyStore(None)
    assert await authorizing_client(fake, store, Clock()).authorize() is True
    assert store.key == fake.issued_key
    assert fake.authorize_body == {"appName": "Jesinia"}
    assert fake.last_headers["zotero-server-id"] == fake.server_id


async def test_should_not_store_a_single_use_key(fake: FakeZotero) -> None:
    fake.authorize_outcome = "once"
    store = MemoryKeyStore(None)
    assert await authorizing_client(fake, store, Clock()).authorize() is False
    assert store.key is None


async def test_should_report_unauthorized_when_authorize_is_denied(fake: FakeZotero) -> None:
    fake.authorize_outcome = "denied"
    with pytest.raises(ZoteroUnavailable) as raised:
        await authorizing_client(fake, MemoryKeyStore(None), Clock()).authorize()
    assert raised.value.state == "unauthorized"


async def test_should_surface_zotero_throttling_of_authorize(fake: FakeZotero) -> None:
    fake.authorize_outcome = "throttled"
    with pytest.raises(AuthorizeTooSoon, match="30"):
        await authorizing_client(fake, MemoryKeyStore(None), Clock()).authorize()


async def test_should_refuse_a_second_authorize_within_a_minute(fake: FakeZotero) -> None:
    clock = Clock()
    api = authorizing_client(fake, MemoryKeyStore(None), clock)
    await api.authorize()
    clock.now += 59
    with pytest.raises(AuthorizeTooSoon):
        await api.authorize()
    clock.now += 2
    assert await api.authorize() is True


async def test_should_wait_for_the_author_to_answer_the_dialog(
    fake: FakeZotero, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    real_client = httpx.AsyncClient

    def spy(**kwargs: Any) -> httpx.AsyncClient:
        seen.append(kwargs["timeout"])
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    await authorizing_client(fake, MemoryKeyStore(None), Clock()).authorize()
    assert seen[-1] >= 300  # the authorize POST is the last request


async def test_should_request_api_prefixed_paths(fake: FakeZotero) -> None:
    seen: list[str] = []

    class Spy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return await fake.transport().handle_async_request(request)

    api = LocalApiClient(BASE_URL + "/", MemoryKeyStore("k"), transport=Spy())
    await api.library_version()
    await api.authorize()
    assert seen[0] == "/api/users/0/items"
    assert seen[-1] == "/api/local/authorize"


@pytest.mark.parametrize(
    "body",
    [
        {"remember": True},
        {"remember": True, "key": None},
        {"remember": True, "key": ""},
        {"remember": True, "key": 12},
        {"remember": "yes", "key": "abc"},
        {"key": "abc"},
    ],
)
async def test_should_reject_a_malformed_authorize_response(
    body: dict[str, object],
) -> None:
    mock = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    store = MemoryKeyStore(None)
    api = LocalApiClient(BASE_URL, store, transport=_with_server_id(mock), clock=Clock())
    with pytest.raises(ZoteroError, match="unexpected authorize response"):
        await api.authorize()
    assert store.key is None
