# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Google Reader API NetNewsWire syncs with (NetNewsWire sync spec), end to end."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jesinia.apps.rss.domain.reader import item_long_id, write_token
from jesinia.core.config import Config
from tests.apps.rss.conftest import KeepEnv, keep_server

G = "/api/greader"
R = f"{G}/reader/api/0"
FIXTURES = Path(__file__).parent / "fixtures"
FEED_URL = "https://example.com/feed.xml"
STARRED = "user/-/state/com.google/starred"
READ = "user/-/state/com.google/read"


@dataclass
class Reader:
    """A signed-in NetNewsWire: its reader token and write token."""

    env: KeepEnv
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"GoogleLogin auth={self.token}"}

    async def get(self, path: str, **params: str | int) -> httpx.Response:
        return await self.env.client.get(f"{R}{path}", params=params, headers=self.auth)

    async def post(self, path: str, body: str, *, with_t: bool = True) -> httpx.Response:
        if with_t:
            body = f"T={write_token(self.token)}&{body}"
        return await self.env.client.post(
            f"{R}{path}",
            content=body,
            headers={**self.auth, "content-type": "application/x-www-form-urlencoded"},
        )

    async def ids(self, **params: str | int) -> list[int]:
        r = await self.get("/stream/items/ids", output="json", **params)
        assert r.status_code == 200, r.text
        return [int(ref["id"]) for ref in r.json().get("itemRefs", [])]


async def _password(env: KeepEnv) -> str:
    r = await env.client.post("/api/rss/reader/password")
    assert r.status_code == 200, r.text
    return str(r.json()["password"])


async def _login(env: KeepEnv, password: str) -> httpx.Response:
    return await env.client.post(
        f"{G}/accounts/ClientLogin",
        content=f"Email=me&Passwd={password}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


async def _sign_in(env: KeepEnv) -> Reader:
    r = await _login(env, await _password(env))
    assert r.status_code == 200, r.text
    lines = dict(line.split("=", 1) for line in r.text.strip().splitlines())
    return Reader(env, lines["Auth"])


@pytest.fixture
async def reader(keep_env: KeepEnv) -> Reader:
    return await _sign_in(keep_env)


def _attempted(env: KeepEnv, post_id: int) -> None:
    env.conn.execute(
        "UPDATE posts SET article_attempted_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), post_id),
    )


def _ledger(env: KeepEnv, post_id: int) -> list[tuple[str, str]]:
    rows = env.conn.execute(
        "SELECT action, source FROM star_ledger WHERE post_id = ? ORDER BY id", (post_id,)
    ).fetchall()
    return [(str(a), str(s)) for a, s in rows]


# -- credentials ---------------------------------------------------------------------------


async def test_reader_setup_should_report_the_server_url_and_password_state(
    keep_env: KeepEnv,
) -> None:
    before = (await keep_env.client.get("/api/rss/reader")).json()
    assert before == {"has_password": False, "server_url": "http://127.0.0.1:5374/api/greader"}
    created = (await keep_env.client.post("/api/rss/reader/password")).json()
    assert created["has_password"] is True and len(created["password"]) >= 32
    assert (await keep_env.client.get("/api/rss/reader")).json()["has_password"] is True


async def test_reader_setup_should_offer_the_tailnet_url_when_remote_access_is_on(
    cfg: Config, kb_root: Path, mock: respx.MockRouter
) -> None:
    from dataclasses import replace

    async with keep_server(replace(cfg, remote_host="mac.tail1.ts.net"), kb_root) as env:
        url = (await env.client.get("/api/rss/reader")).json()["server_url"]
        assert url == "https://mac.tail1.ts.net/api/greader"


async def test_client_login_should_refuse_a_wrong_or_missing_password(keep_env: KeepEnv) -> None:
    assert (await _login(keep_env, "anything")).status_code == 401  # none created yet
    await _password(keep_env)
    refused = await _login(keep_env, "wrong")
    assert refused.status_code == 401 and "ReaderAuthFailed" in refused.text


async def test_client_login_should_never_take_the_password_from_the_url(
    keep_env: KeepEnv,
) -> None:
    password = await _password(keep_env)
    r = await keep_env.client.post(f"{G}/accounts/ClientLogin", params={"Passwd": password})
    assert r.status_code == 401
    assert (await keep_env.client.get(f"{G}/accounts/ClientLogin")).status_code == 405


async def test_bodies_should_be_capped(reader: Reader) -> None:
    from jesinia.apps.rss.reader_api import FORM_MAX

    big = "i=1&" * (FORM_MAX // 4 + 1)
    declared = await reader.post("/edit-tag", big)
    assert declared.status_code == 413 and "BodyTooLarge" in declared.text

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * (FORM_MAX // 2)

    streamed = await reader.env.client.post(f"{R}/edit-tag", content=chunks(), headers=reader.auth)
    assert streamed.status_code == 413


async def test_a_new_password_should_revoke_earlier_sign_ins(keep_env: KeepEnv) -> None:
    first = await _sign_in(keep_env)
    assert (await first.get("/user-info")).status_code == 200
    second = await _sign_in(keep_env)
    assert (await first.get("/user-info")).status_code == 401
    assert (await second.get("/user-info")).json()["userName"] == "jesinia"


async def test_reader_routes_should_need_a_reader_token(keep_env: KeepEnv, cfg: Config) -> None:
    await _sign_in(keep_env)
    for headers in (
        {},
        {"Authorization": "GoogleLogin nope"},
        {"Authorization": "GoogleLogin auth=forged"},
        # The install token is not a reader token: the Reader API reaches nothing else.
        {"Authorization": f"GoogleLogin auth={cfg.token}"},
    ):
        r = await keep_env.client.get(f"{R}/subscription/list", headers=headers)
        assert r.status_code == 401, headers


async def test_writes_should_need_the_write_token(reader: Reader) -> None:
    token = await reader.get("/token")
    assert token.text == write_token(reader.token)
    post_id = reader.env.post()
    without = await reader.post("/edit-tag", f"i={post_id}&a={READ}", with_t=False)
    assert without.status_code == 401 and "WriteTokenRejected" in without.text
    stale = await reader.post("/edit-tag", f"T=old&i={post_id}&a={READ}", with_t=False)
    assert stale.status_code == 401


# -- feeds and folders -----------------------------------------------------------------------


async def test_tag_and_subscription_lists_should_mirror_feeds_and_folders(reader: Reader) -> None:
    env = reader.env
    reader.env.post(feed="https://a.example/feed.xml")
    env.post(feed="https://b.example/feed.xml")
    feeds = (await env.client.get("/api/rss/feeds")).json()
    first = next(f for f in feeds if f["url"] == "https://a.example/feed.xml")
    await env.client.patch(f"/api/rss/feeds/{first['id']}", json={"folder": "Tech"})

    tags = (await reader.get("/tag/list", output="json")).json()["tags"]
    assert tags == [{"id": STARRED}, {"id": "user/-/label/Tech", "type": "folder"}]
    subs = {s["url"]: s for s in (await reader.get("/subscription/list")).json()["subscriptions"]}
    assert subs["https://a.example/feed.xml"]["categories"] == [
        {"id": "user/-/label/Tech", "label": "Tech"}
    ]
    assert subs["https://a.example/feed.xml"]["id"] == f"feed/{first['id']}"
    assert subs["https://b.example/feed.xml"]["categories"] == []


async def test_quick_add_should_subscribe_or_find_the_feed(
    reader: Reader, mock: respx.MockRouter
) -> None:
    mock.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=(FIXTURES / "rss2.xml").read_bytes(),
            headers={"content-type": "application/rss+xml"},
        )
    )
    added = (await reader.post("/subscription/quickadd", f"quickadd={FEED_URL}")).json()
    assert added["numResults"] == 1 and added["streamName"] == "Example Blog"
    again = (await reader.post("/subscription/quickadd", f"quickadd=feed/{FEED_URL}")).json()
    assert again["streamId"] == added["streamId"]
    bad = (await reader.post("/subscription/quickadd", "quickadd=not-a-url")).json()
    assert bad["numResults"] == 0 and "error" in bad


async def test_subscription_edit_should_rename_move_unfile_and_unsubscribe(reader: Reader) -> None:
    env = reader.env
    env.post()
    feed_id = (await env.client.get("/api/rss/feeds")).json()[0]["id"]
    s = f"s=feed/{feed_id}"

    assert (await reader.post("/subscription/edit", f"{s}&ac=edit&t=Fly")).text == "OK"
    assert (
        await reader.post("/subscription/edit", f"{s}&ac=edit&a=user/-/label/Db")
    ).status_code == 200
    moved = (await env.client.get("/api/rss/feeds")).json()[0]
    assert moved["title"] == "Fly" and moved["folder"] == "Db"
    move = f"{s}&ac=edit&r=user/-/label/Db&a=user/-/label/Data"
    await reader.post("/subscription/edit", move)
    assert (await env.client.get("/api/rss/feeds")).json()[0]["folder"] == "Data"
    await reader.post("/subscription/edit", f"{s}&ac=edit&r=user/-/label/Elsewhere")
    assert (await env.client.get("/api/rss/feeds")).json()[0]["folder"] == "Data"
    await reader.post("/subscription/edit", f"{s}&ac=edit&r=user/-/label/Data")
    assert (await env.client.get("/api/rss/feeds")).json()[0]["folder"] is None

    blank = await reader.post("/subscription/edit", f"{s}&ac=edit&t=%20")
    assert blank.status_code == 400 and "InvalidFeedTitle" in blank.text
    not_feed = await reader.post("/subscription/edit", f"s={STARRED}&ac=edit&t=x")
    assert not_feed.status_code == 400
    assert (await reader.post("/subscription/edit", f"{s}&ac=unsubscribe")).text == "OK"
    assert (await env.client.get("/api/rss/feeds")).json() == []
    gone = await reader.post("/subscription/edit", f"{s}&ac=edit&t=x")
    assert gone.status_code == 404


async def test_subscription_edit_should_subscribe_by_url(
    reader: Reader, mock: respx.MockRouter
) -> None:
    mock.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=(FIXTURES / "rss2.xml").read_bytes(),
            headers={"content-type": "application/rss+xml"},
        )
    )
    body = f"s=feed/{FEED_URL}&ac=subscribe&a=user/-/label/News"
    assert (await reader.post("/subscription/edit", body)).text == "OK"
    feeds = (await reader.env.client.get("/api/rss/feeds")).json()
    assert [(f["url"], f["folder"]) for f in feeds] == [(FEED_URL, "News")]


async def test_folders_should_rename_and_delete(reader: Reader) -> None:
    env = reader.env
    env.post()
    feed_id = (await env.client.get("/api/rss/feeds")).json()[0]["id"]
    await reader.post("/subscription/edit", f"s=feed/{feed_id}&ac=edit&a=user/-/label/Old")
    rename = "s=user/-/label/Old&dest=user/-/label/New"
    assert (await reader.post("/rename-tag", rename)).text == "OK"
    assert (await env.client.get("/api/rss/feeds")).json()[0]["folder"] == "New"
    assert (await reader.post("/disable-tag", "s=user/-/label/New")).text == "OK"
    assert (await env.client.get("/api/rss/feeds")).json()[0]["folder"] is None

    assert (await reader.post("/disable-tag", "s=user/-/label/New")).status_code == 404
    assert (await reader.post("/disable-tag", f"s={STARRED}")).status_code == 400
    assert (await reader.post("/rename-tag", f"s={STARRED}&dest=x")).status_code == 400


async def test_opml_import_should_subscribe_without_a_write_token(reader: Reader) -> None:
    opml = (
        '<?xml version="1.0"?><opml version="2.0"><body>'
        '<outline text="Tech"><outline type="rss" text="X" xmlUrl="https://x.example/f.xml"/>'
        "</outline></body></opml>"
    )
    r = await reader.env.client.post(
        f"{R}/subscription/import",
        content=opml,
        headers={**reader.auth, "content-type": "text/xml"},
    )
    assert r.text == "OK"
    feeds = (await reader.env.client.get("/api/rss/feeds")).json()
    assert [(f["url"], f["folder"]) for f in feeds] == [("https://x.example/f.xml", "Tech")]


# -- item lists ------------------------------------------------------------------------------


async def test_item_ids_should_filter_by_stream_state_and_time(reader: Reader) -> None:
    env = reader.env
    a = env.post(url="https://fly.io/a")
    b = env.post(url="https://fly.io/b", feed="https://other.example/feed.xml")
    c = env.post(url="https://fly.io/c")
    await env.client.post("/api/rss/posts/read-state", json={"ids": [b], "read": True})
    feeds = {f["url"]: f["id"] for f in (await env.client.get("/api/rss/feeds")).json()}
    other = feeds["https://other.example/feed.xml"]
    await env.client.patch(f"/api/rss/feeds/{other}", json={"folder": "Other"})

    assert await reader.ids(s="user/-/state/com.google/reading-list", n=100) == [c, b, a]
    assert await reader.ids(xt=READ, n=100) == [c, a]
    assert await reader.ids(s=READ) == [b]
    assert await reader.ids(s=f"feed/{other}") == [b]
    assert await reader.ids(s="user/-/label/Other") == [b]
    assert await reader.ids(s=STARRED) == []
    assert await reader.ids(xt="user/-/state/com.google/reading-list") == []
    future = int((datetime.now(UTC) + timedelta(days=1)).timestamp())
    assert await reader.ids(ot=future) == []
    assert await reader.ids(ot=0) == [c, b, a]


async def test_item_ids_should_page_with_a_continuation(reader: Reader) -> None:
    ids = sorted((reader.env.post(url=f"https://fly.io/{n}") for n in range(5)), reverse=True)
    first = (await reader.get("/stream/items/ids", n=2)).json()
    assert [int(r["id"]) for r in first["itemRefs"]] == ids[:2]
    second = (await reader.get("/stream/items/ids", n=2, c=first["continuation"])).json()
    third = (await reader.get("/stream/items/ids", n=2, c=second["continuation"])).json()
    assert [int(r["id"]) for r in third["itemRefs"]] == ids[4:] and "continuation" not in third


@pytest.mark.parametrize(
    "params",
    [{"s": "bogus"}, {"n": "many"}, {"ot": "yesterday"}, {"xt": "feed/1"}, {"c": "x"}],
)
async def test_item_ids_should_refuse_unreadable_parameters(
    reader: Reader, params: dict[str, str]
) -> None:
    r = await reader.get("/stream/items/ids", **params)
    assert r.status_code == 400


async def test_new_posts_should_wait_for_their_full_article(reader: Reader) -> None:
    env = reader.env
    post_id = env.post()
    now = datetime.now(UTC).isoformat()
    env.conn.execute("UPDATE posts SET first_seen_at = ? WHERE id = ?", (now, post_id))
    assert await reader.ids() == []  # held: the prefetch has not tried it yet
    _attempted(env, post_id)
    assert await reader.ids() == [post_id]


async def test_unsubscribed_feeds_should_drop_out_of_item_lists(reader: Reader) -> None:
    env = reader.env
    env.post()
    feed_id = (await env.client.get("/api/rss/feeds")).json()[0]["id"]
    await env.client.delete(f"/api/rss/feeds/{feed_id}")
    assert await reader.ids() == []


# -- item contents ---------------------------------------------------------------------------


async def test_contents_should_carry_the_full_article_and_state(reader: Reader) -> None:
    env = reader.env
    full = env.post(url="https://fly.io/full")
    env.cache_article(full, "# Title\n\nSee [the docs](/docs) and <script>x</script>.")
    feed_only = env.post(url="https://fly.io/feed-only")
    bare = env.post(url="https://fly.io/bare", content="")
    await env.client.post("/api/rss/posts/read-state", json={"ids": [full], "read": True})

    body = f"i={item_long_id(full)}&i={feed_only}&i={bare}&i=999999"
    r = await reader.post("/stream/items/contents", body, with_t=False)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data["updated"], int) and data["id"].endswith("reading-list")
    items = {item["id"]: item for item in data["items"]}
    assert set(items) == {item_long_id(full), item_long_id(feed_only), item_long_id(bare)}

    article = items[item_long_id(full)]
    html = article["summary"]["content"]
    assert '<a href="https://fly.io/docs"' in html and "<h1>Title</h1>" in html
    assert "<script>" not in html
    assert READ in article["categories"]
    assert article["origin"]["streamId"].startswith("feed/")
    assert article["alternate"] == [{"href": "https://fly.io/full", "type": "text/html"}]
    assert article["author"] == "Ben Johnson"
    assert items[item_long_id(feed_only)]["summary"]["content"] == (
        "<p>The feed's own summary of the post.</p>"
    )
    assert READ not in items[item_long_id(feed_only)]["categories"]
    assert items[item_long_id(bare)]["summary"]["content"] == "<p>The feed&#x27;s own summary</p>"


async def test_contents_should_refuse_unreadable_item_ids(reader: Reader) -> None:
    r = await reader.get("/stream/items/contents", i="tag:google.com,2005:reader/item/zz")
    assert r.status_code == 400 and "InvalidItemId" in r.text


# -- read state and stars ----------------------------------------------------------------


async def test_read_and_unread_should_set_the_reader_read_state(reader: Reader) -> None:
    env = reader.env
    post_id = env.post()
    i = f"i={item_long_id(post_id)}"
    assert (await reader.post("/edit-tag", f"{i}&a={READ}")).text == "OK"
    assert (await env.client.get("/api/rss/posts?view=unread")).json()["items"] == []
    await reader.post("/edit-tag", f"{i}&r={READ}&a=user/-/label/ignored")
    assert len((await env.client.get("/api/rss/posts?view=unread")).json()["items"]) == 1


async def test_star_should_keep_and_unstar_should_only_be_recorded(reader: Reader) -> None:
    env = reader.env
    post_id = env.post()
    i = f"i={item_long_id(post_id)}"

    assert (await reader.post("/edit-tag", f"{i}&a={STARRED}")).text == "OK"
    page_id, _, _ = env.state(post_id)
    assert page_id is not None and env.kb.index.by_source("https://fly.io/blog/sqlite-wal")
    assert _ledger(env, post_id) == [("keep", "netnewswire")]
    assert await reader.ids(s=STARRED) == [post_id]
    await reader.post("/edit-tag", f"{i}&a={STARRED}")  # already starred: nothing new
    assert _ledger(env, post_id) == [("keep", "netnewswire")]

    await reader.post("/edit-tag", f"{i}&r={STARRED}")
    assert _ledger(env, post_id) == [("keep", "netnewswire"), ("unkeep", "netnewswire")]
    assert env.state(post_id)[0] == page_id  # the page and the Kept view stay
    assert await reader.ids(s=STARRED) == []
    assert await reader.ids(xt=STARRED) == [post_id]
    await reader.post("/edit-tag", f"{i}&r={STARRED}")  # already unstarred
    assert len(_ledger(env, post_id)) == 2

    await env.rss.keeper.drain()
    pages_before = sorted(p.name for p in env.root.rglob("*.md"))
    await reader.post("/edit-tag", f"{i}&a={STARRED}")
    assert _ledger(env, post_id)[-1] == ("keep", "netnewswire")
    assert sorted(p.name for p in env.root.rglob("*.md")) == pages_before  # no new page


async def test_stars_should_skip_posts_that_are_gone(reader: Reader) -> None:
    body = f"i={item_long_id(424242)}&a={STARRED}&r={STARRED}"
    assert (await reader.post("/edit-tag", body)).text == "OK"


async def test_a_reader_keep_should_count_as_a_star(reader: Reader) -> None:
    env = reader.env
    post_id = env.post()
    assert (await env.keep(post_id, "worth it")).status_code in (200, 201)
    assert _ledger(env, post_id) == [("keep", "reader")]
    assert await reader.ids(s=STARRED) == [post_id]
    starred = (await reader.post("/stream/items/contents", f"i={post_id}", with_t=False)).json()
    assert STARRED in starred["items"][0]["categories"]


async def test_star_should_report_a_missing_kb(cfg: Config, mock: respx.MockRouter) -> None:
    async with keep_server(cfg, None) as env:
        reader = await _sign_in(env)
        post_id = env.post()
        r = await reader.post("/edit-tag", f"i={post_id}&a={STARRED}")
        assert r.status_code == 503
        assert _ledger(env, post_id) == []


async def test_item_ids_should_list_newest_first_by_post_time(reader: Reader) -> None:
    env = reader.env
    older = env.post(url="https://fly.io/older")
    newer = env.post(url="https://fly.io/newer")
    # Inserted in feed order, newest first: the newer post can have the lower id.
    env.conn.execute(
        "UPDATE posts SET published_at = ? WHERE id = ?", ("2026-10-01T00:00:00+00:00", newer)
    )
    env.conn.execute(
        "UPDATE posts SET published_at = ? WHERE id = ?", ("2026-10-20T00:00:00+00:00", older)
    )
    first = (await reader.get("/stream/items/ids", n=1)).json()
    assert [int(r["id"]) for r in first["itemRefs"]] == [older]
    assert "+" not in first["continuation"]
    second = (await reader.get("/stream/items/ids", n=1, c=first["continuation"])).json()
    assert [int(r["id"]) for r in second["itemRefs"]] == [newer]


async def test_post_ids_should_never_be_reused_after_pruning(reader: Reader) -> None:
    env = reader.env
    gone = env.post(url="https://fly.io/gone")
    env.conn.execute("DELETE FROM posts WHERE id = ?", (gone,))
    assert env.post(url="https://fly.io/next") > gone


async def test_quick_add_should_find_a_subscribed_feed_by_its_site(
    reader: Reader, mock: respx.MockRouter
) -> None:
    site = "https://example.com/"
    link = f'<link rel="alternate" type="application/rss+xml" href="{FEED_URL}">'
    page = f"<html><head>{link}</head></html>"
    mock.get(site).mock(
        return_value=httpx.Response(
            200, content=page.encode(), headers={"content-type": "text/html"}
        )
    )
    mock.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=(FIXTURES / "rss2.xml").read_bytes(),
            headers={"content-type": "application/rss+xml"},
        )
    )
    first = (await reader.post("/subscription/quickadd", f"quickadd={FEED_URL}")).json()
    by_site = (await reader.post("/subscription/quickadd", f"quickadd={site}")).json()
    assert by_site["numResults"] == 1 and by_site["streamId"] == first["streamId"]
