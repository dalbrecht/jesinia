# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tomllib
from dataclasses import replace as replace_config
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

import httpx
import pytest
import respx
import yaml
from fastapi import HTTPException
from markdown_it import MarkdownIt

from jesinia.apps.rss.application.keep import ARCHIVE_CONCURRENCY
from jesinia.apps.rss.domain.kept import (
    ARCHIVE_MAX_ATTEMPTS,
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_IMAGES,
    FEED_FALLBACK_NOTE,
    archive_state,
    find_images,
    is_http_url,
    kept_again,
    kept_front_matter,
    kept_title,
    render_kept_body,
    replace_article,
    rewrite_images,
)
from jesinia.apps.rss.domain.post import PostView
from jesinia.apps.rss.domain.settings import InvalidSettings, validate_settings
from jesinia.apps.rss.module import RssModule
from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.events import Event
from jesinia.services.fetch.application.service import FetchError, FetchService
from jesinia.services.kb.application.errors import StaleWrite
from jesinia.services.kb.application.service import PageDraft
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.tags import extract_tags
from tests.apps.rss.conftest import KEEP_API, PUBLISHED, KeepEnv, keep_server
from tests.apps.rss.test_posts import ARTICLE_FIXTURE, StubFetchModule
from tests.conftest import running

PNG = "image/png"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"


def _front_matter(text: str) -> dict[str, object]:
    _, raw, _ = text.split("---\n", 2)
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict)
    return loaded


def _html(*paragraphs: str) -> bytes:
    filler = (
        "<p>An article long enough for the extractor to keep it as the main text of the page, "
        "with a few more words added so it clearly counts as prose and not navigation.</p>"
    )
    body = "".join(paragraphs)
    return f"<html><body><article><h1>T</h1>{filler}{body}{filler}</article></body></html>".encode()


# --- the kept page --------------------------------------------------------------------------


async def test_keep_writes_source_page_in_spec_order(keep_env: KeepEnv) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "The whole article, as Markdown.")
    kept = await keep_env.keep(post_id, "Good explainer on checkpoints.", ["SQLite", "databases"])
    assert kept.status_code == 201, kept.text
    result = kept.json()
    assert result["created"] is True
    assert result["path"] == "Sources/What SQLite's WAL mode actually guarantees.md"
    assert result["post"]["kept_page_id"] == result["page_id"]
    assert result["post"]["archive_state"] == "archiving"

    text = keep_env.page_text(result["path"])
    front = _front_matter(text)
    assert front["id"] == result["page_id"]
    assert front["title"] == "What SQLite's WAL mode actually guarantees"
    jesinia = front["jesinia"]
    assert isinstance(jesinia, dict)
    assert list(jesinia) == ["kind", "source", "feed", "author", "published", "captured"]
    assert jesinia["kind"] == "source/rss"
    assert jesinia["source"] == "https://fly.io/blog/sqlite-wal"
    assert jesinia["feed"] == "https://fly.io/blog/feed.xml"
    assert jesinia["author"] == "Ben Johnson"
    assert jesinia["published"] == date(2026, 10, 14)
    assert datetime.fromisoformat(str(jesinia["captured"])).tzinfo is not None

    body = text.split("---\n", 2)[2]
    assert body == (
        "Good explainer on checkpoints.\n\n"
        "Tags: #sqlite #databases\n\n"
        "## Article\n\n"
        "The whole article, as Markdown.\n"
    )
    # New tags join the catalog before the page is written.
    assert "- #sqlite" in keep_env.page_text("Tags.md")
    assert "- #databases" in keep_env.page_text("Tags.md")

    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id) == (result["page_id"], "done", None)
    assert keep_env.page_text(result["path"]) == text  # nothing to add: no images


async def test_keep_without_a_full_article_archives_the_feed_content_with_a_note(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    mock.get("https://fly.io/blog/sqlite-wal").mock(return_value=httpx.Response(404))
    post_id = keep_env.post(content="<p>The feed's <em>own</em> summary.</p>")
    kept = await keep_env.keep(post_id)
    assert kept.status_code == 201, kept.text
    body = keep_env.page_text(kept.json()["path"]).split("---\n", 2)[2]
    # No note and no tags: the page starts at the article.
    assert body == f"## Article\n\n_{FEED_FALLBACK_NOTE}_\n\nThe feed's *own* summary.\n"


async def test_keep_falls_back_to_the_snippet_as_plain_text(
    keep_env: KeepEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "Click [here](javascript:alert(1)) *now* <b>"
    bare = keep_env.post(url="https://example.org/no-content", content="")
    keep_env.conn.execute("UPDATE posts SET snippet = ? WHERE id = ?", (hostile, bare))
    fetch = keep_env.app.state.ctx.services["fetch"]
    assert isinstance(fetch, FetchService)

    async def broken(html: bytes, url: str) -> None:
        raise FetchError("could not extract the article: parser exploded")

    monkeypatch.setattr(fetch, "convert", broken)
    failing = keep_env.post(url="https://example.org/bad-content")
    keep_env.conn.execute("UPDATE posts SET snippet = ? WHERE id = ?", (hostile, failing))
    for post_id in (bare, failing):
        kept = await keep_env.keep(post_id)
        assert kept.status_code == 201, kept.text  # Keep never fails because extraction did
        article = keep_env.page_text(kept.json()["path"]).split("## Article\n\n", 1)[1]
        assert "Click \\[here\\](javascript:alert(1)) \\*now\\* \\<b\\>" in article
        tokens = MarkdownIt("commonmark").parseInline(article)
        assert not [t for t in tokens[0].children or [] if t.type == "link_open"]


async def test_keeping_while_the_archive_runs_starts_no_second_archive(
    keep_env: KeepEnv,
) -> None:
    first = keep_env.post(feed="https://fly.io/blog/feed.xml")
    second = keep_env.post(feed="https://planet.example/feed.xml")
    keep_env.cache_article(first, "Article text.")
    keeper = keep_env.rss.keeper
    one = await keeper.keep(first, "One.", [], None)
    assert keeper.pending() == [one.page_id]
    two = await keeper.keep(second, "Two.", [], None)
    assert two.page_id == one.page_id and not two.created
    assert keeper.pending() == [one.page_id]  # the running archive covers both posts
    await keeper.drain()
    assert keep_env.state(first)[1] == keep_env.state(second)[1] == "done"


async def test_keep_again_after_a_failed_archive_archives_again(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    route = mock.get("https://fly.io/blog/sqlite-wal").mock(return_value=httpx.Response(500))
    post_id = keep_env.post()
    kept = await keep_env.keep(post_id, "First.")
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "failed"
    route.mock(
        return_value=httpx.Response(
            200,
            content=_html("<p>The article, at last.</p>"),
            headers={"content-type": "text/html"},
        )
    )
    again = await keep_env.keep(post_id, "Second.")
    assert again.json()["post"]["archive_state"] == "archiving"
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done"
    assert "The article, at last." in keep_env.page_text(kept.json()["path"])


async def test_keep_of_a_source_page_this_reader_never_archived_leaves_it_alone(
    keep_env: KeepEnv,
) -> None:
    # A page for the URL that came from elsewhere (another Mac's reader, or by hand).
    await keep_env.kb.write(
        PageDraft(
            None, "Hand made", PurePosixPath("Reading"), "## Article\n\nMine.\n",
            {"kind": "source/rss", "source": "https://fly.io/blog/sqlite-wal"},
        )
    )  # fmt: skip
    post_id = keep_env.post()
    kept = await keep_env.keep(post_id, "Noted.")
    assert kept.status_code == 200 and kept.json()["path"] == "Reading/Hand made.md"
    assert keep_env.state(post_id)[1] == "done" and keep_env.rss.keeper.pending() == []
    assert keep_env.page_text("Reading/Hand made.md").endswith("## Article\n\nMine.\n")


async def test_keep_reports_an_unwritable_source_folder(keep_env: KeepEnv) -> None:
    ctx = keep_env.app.state.ctx
    ctx.cfg = replace_config(ctx.cfg, folders={**ctx.cfg.folders, "source": "Journal/Kept"})
    post_id = keep_env.post()
    refused = await keep_env.keep(post_id)
    assert refused.status_code == 400 and refused.json()["error"] == "ReservedPath"
    assert keep_env.state(post_id) == (None, None, None)


async def test_keep_cannot_escape_the_source_folder_through_a_title(keep_env: KeepEnv) -> None:
    post_id = keep_env.post(title="../../../etc/passwd")
    keep_env.cache_article(post_id, "x")
    kept = await keep_env.keep(post_id)
    assert kept.status_code == 201, kept.text
    path = PurePosixPath(kept.json()["path"])
    assert path.parent == PurePosixPath("Sources") and ".." not in path.parts
    assert (keep_env.root / path).is_file()


async def test_keep_from_two_feeds_updates_one_page(keep_env: KeepEnv) -> None:
    first = keep_env.post(feed="https://fly.io/blog/feed.xml")
    second = keep_env.post(feed="https://planet.example/feed.xml")
    keep_env.cache_article(first, "Article text.")
    one = await keep_env.keep(first, "From Fly.")
    await keep_env.rss.keeper.drain()
    two = await keep_env.keep(second, "From the planet.")
    assert two.status_code == 200, two.text
    assert one.json()["page_id"] == two.json()["page_id"]
    assert two.json()["created"] is False
    assert sorted(p.name for p in (keep_env.root / "Sources").iterdir()) == [
        "What SQLite's WAL mode actually guarantees.md"
    ]
    # The page was archived already, so the second post shares that state.
    assert keep_env.state(second) == (one.json()["page_id"], "done", None)


async def test_keep_again_appends_note_and_merges_tags(keep_env: KeepEnv) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "Article text.")
    first = await keep_env.keep(post_id, "First thoughts.", ["sqlite", "wal"])
    await keep_env.rss.keeper.drain()
    before = datetime.now().astimezone().date().isoformat()
    again = await keep_env.keep(post_id, "Second thoughts.", ["WAL", "durability"])
    after = datetime.now().astimezone().date().isoformat()
    body = keep_env.page_text(first.json()["path"]).split("---\n", 2)[2]
    dated = re.search(r"_Kept again (\d{4}-\d{2}-\d{2}):_", body)
    assert dated is not None and dated.group(1) in (before, after)  # even across midnight
    today = dated.group(1)
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert body == (
        "First thoughts.\n\n"
        f"_Kept again {today}:_ Second thoughts.\n\n"
        "Tags: #sqlite #wal #durability\n\n"
        "## Article\n\n"
        "Article text.\n"
    )


async def test_keep_again_does_not_rearchive(keep_env: KeepEnv, mock: respx.MockRouter) -> None:
    article = mock.get("https://fly.io/blog/sqlite-wal").mock(
        return_value=httpx.Response(
            200, content=ARTICLE_FIXTURE.read_bytes(), headers={"content-type": "text/html"}
        )
    )
    post_id = keep_env.post()
    kept = await keep_env.keep(post_id, "Mine.")
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done" and article.call_count == 1
    # The author edits the archived article; keeping again must not overwrite it.
    page = await keep_env.kb.read(PageId(kept.json()["page_id"]))
    edited = page.page.body.replace("Most personal tools", "MY EDIT: most personal tools")
    await keep_env.kb.write(
        PageDraft(page.page.id, page.page.title, page.page.path.parent, edited, page.page.jesinia),
        expected_rev=page.rev,
    )
    again = await keep_env.keep(post_id, "More.")
    assert again.json()["post"]["archive_state"] == "done"
    await keep_env.rss.keeper.drain()
    assert "MY EDIT: most personal tools" in keep_env.page_text(kept.json()["path"])
    assert article.call_count == 1


async def test_hn_keep_records_discussion(keep_env: KeepEnv) -> None:
    post_id = keep_env.post(
        url="https://example.org/linked-article",
        feed="https://news.ycombinator.com/rss",
        title="Linked article",
        discussion="https://news.ycombinator.com/item?id=4242",
    )
    keep_env.cache_article(post_id, "The linked article's text.")
    kept = await keep_env.keep(post_id)
    jesinia = _front_matter(keep_env.page_text(kept.json()["path"]))["jesinia"]
    assert isinstance(jesinia, dict)
    assert jesinia["source"] == "https://example.org/linked-article"
    assert jesinia["discussion"] == "https://news.ycombinator.com/item?id=4242"
    assert "The linked article's text." in keep_env.page_text(kept.json()["path"])


async def test_keep_refuses_a_near_duplicate_tag_and_writes_nothing(keep_env: KeepEnv) -> None:
    await keep_env.rss.keeper.tags.add("database")
    post_id = keep_env.post()
    refused = await keep_env.keep(post_id, "x", ["databse"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == "NearDuplicate"
    assert refused.json()["near"] == ["database"]
    assert not (keep_env.root / "Sources").exists()
    assert keep_env.state(post_id) == (None, None, None)
    invalid = await keep_env.keep(post_id, "x", ["#!"])
    assert invalid.status_code == 400 and invalid.json()["error"] == "InvalidTag"


async def test_keep_errors_and_owner_only(keep_env: KeepEnv) -> None:
    missing = await keep_env.keep(9999)
    assert missing.status_code == 404 and missing.json()["error"] == "PostNotFound"
    retry_missing = await keep_env.client.post(f"{KEEP_API}/posts/9999/archive")
    assert retry_missing.status_code == 404
    post_id = keep_env.post()
    not_kept = await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
    assert not_kept.status_code == 409 and not_kept.json()["error"] == "PostNotKept"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=keep_env.app), base_url="http://127.0.0.1:5374"
    ) as anonymous:
        for call in (
            anonymous.post(f"{KEEP_API}/posts/{post_id}/keep", json={"note": "", "tags": []}),
            anonymous.post(f"{KEEP_API}/posts/{post_id}/archive"),
            anonymous.get(f"{KEEP_API}/settings"),
        ):
            assert (await call).status_code == 401


async def test_keep_is_refused_while_the_kb_is_unavailable(
    cfg: Config, mock: respx.MockRouter
) -> None:
    async with keep_server(cfg, None) as env:
        post_id = env.post()
        refused = await env.keep(post_id, "A note.", ["sqlite"])
        assert refused.status_code == 503, refused.text
        assert refused.json() == {"error": "KbUnavailable", "detail": "no KB path configured"}
        # Nothing queued: the post is not kept and no archive is pending.
        assert env.state(post_id) == (None, None, None)
        assert env.rss.keeper.pending() == []
        assert await env.rss.keeper.tags.catalog() == []  # "sqlite" was not catalogued


async def test_keep_retry_with_same_idempotency_key_writes_once(keep_env: KeepEnv) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "Article text.")
    first = await keep_env.keep(post_id, "Only once.", ["sqlite"], key="keep-1")
    retry = await keep_env.keep(post_id, "Only once.", ["sqlite"], key="keep-1")
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    text = keep_env.page_text(first.json()["path"])
    assert text.count("Only once.") == 1 and "Kept again" not in text
    unkeyed = await keep_env.client.post(
        f"{KEEP_API}/posts/{post_id}/keep",
        json={"note": "", "tags": []},
        headers={"Idempotency-Key": ""},
    )
    assert unkeyed.status_code == 400


# --- archive --------------------------------------------------------------------------------


async def test_archive_saves_images_with_gitignore_and_caps(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    count = ARCHIVE_MAX_IMAGES + 2
    smalls = [f"https://cdn.example/../../evil/{n}.png?x=1" for n in range(count)]
    for n, url in enumerate(smalls):
        body = (PNG_MAGIC if n % 2 else JPEG_MAGIC) + f"small {n}".encode()
        # The type a server claims is ignored: the bytes decide.
        mock.get(url).mock(
            return_value=httpx.Response(200, content=body, headers={"content-type": "text/plain"})
        )
    lines = [
        "![not http](ftp://img.example/a.png)",
        "![script](javascript:alert(1))",
    ]
    lines += [f"![small {n}](<{url}>)" for n, url in enumerate(smalls)]
    lines.append("[a link, not an image](https://cdn.example/evil/1.png?x=1)")
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "\n\n".join(lines))
    kept = await keep_env.keep(post_id)
    page_id = kept.json()["page_id"]
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id) == (page_id, "done", None)

    assets = keep_env.root / "_assets" / page_id
    assert (assets / ".gitignore").read_text().strip() == "*"
    files = sorted(p for p in assets.iterdir() if p.name != ".gitignore")
    assert len(files) == ARCHIVE_MAX_IMAGES
    # Names come from the content hash and the sniffed type, never from the URL path.
    for path in files:
        assert re.fullmatch(r"[0-9a-f]{32}\.(png|jpg)", path.name), path.name
        assert path.name.startswith(hashlib.sha256(path.read_bytes()).hexdigest()[:32])
        assert path.suffix == (".png" if path.read_bytes().startswith(PNG_MAGIC) else ".jpg")
    assert not (keep_env.root / "evil").exists()

    article = keep_env.page_text(kept.json()["path"]).split("## Article\n\n", 1)[1]
    local = re.findall(r"\]\((\.\./_assets/[^)]+)\)", article)
    assert len(local) == ARCHIVE_MAX_IMAGES
    assert all((keep_env.root / "Sources" / rel).resolve().is_file() for rel in local)
    # The 20-attempt cap left the last two remote, and never fetched them.
    assert [f"](<{url}>)" in article for url in smalls[-2:]] == [True, True]
    assert sum(route.call_count for route in mock.routes) == ARCHIVE_MAX_IMAGES
    # Non-http(s) targets and links are left as they were, never fetched.
    assert "![not http](ftp://img.example/a.png)" in article
    assert "![script](javascript:alert(1))" in article
    assert "[a link, not an image](https://cdn.example/evil/1.png?x=1)" in article


async def test_archive_stops_after_20_attempts_even_when_none_is_an_image(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    pages = mock.get(url__regex=r"https://junk\.example/\d+").mock(
        return_value=httpx.Response(200, content=b"<html>", headers={"content-type": PNG})
    )
    post_id = keep_env.post()
    keep_env.cache_article(
        post_id, "\n\n".join(f"![{n}](https://junk.example/{n})" for n in range(50))
    )
    kept = await keep_env.keep(post_id)
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done"
    assert pages.call_count == ARCHIVE_MAX_ATTEMPTS == 20
    assert not (keep_env.root / "_assets" / kept.json()["page_id"]).exists()


async def test_archive_byte_budget_counts_rejected_bodies(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    junk = b"<html>" + b"x" * (4 * 1024 * 1024)
    bigs = [mock.get(f"https://big.example/{n}").mock(
        return_value=httpx.Response(200, content=junk, headers={"content-type": PNG})
    ) for n in range(3)]  # fmt: skip
    image = mock.get("https://img.example/real.png").mock(
        return_value=httpx.Response(200, content=PNG_MAGIC + b"tiny", headers={"content-type": PNG})
    )
    lines = [f"![{n}](https://big.example/{n})" for n in range(3)]
    lines.append("![real](https://img.example/real.png)")
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "\n\n".join(lines))
    kept = await keep_env.keep(post_id)
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done"
    # 8 MB of rejected bodies, then the third is over the 2 MB left: the budget is spent.
    assert [route.call_count for route in bigs] == [1, 1, 1]
    assert image.call_count == 0
    assert not (keep_env.root / "_assets" / kept.json()["page_id"]).exists()
    assert ARCHIVE_MAX_BYTES == 10 * 1024 * 1024


async def test_archive_saves_only_what_the_bytes_say_is_an_image(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    served = {
        "https://img.example/fake.png": (b"<script>alert(1)</script>", PNG),
        "https://img.example/vector.svg": (b"<svg onload='x'/>", "image/svg+xml"),
        "https://img.example/a.gif": (b"GIF89a" + b"g", "application/octet-stream"),
        "https://img.example/a.webp": (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "image/webp"),
        "https://img.example/a.avif": (b"\x00\x00\x00\x1cftypavif" + b"a", "image/avif"),
    }
    for url, (body, kind) in served.items():
        mock.get(url).mock(
            return_value=httpx.Response(200, content=body, headers={"content-type": kind})
        )
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "\n\n".join(f"![x]({url})" for url in served))
    kept = await keep_env.keep(post_id)
    await keep_env.rss.keeper.drain()
    assets = keep_env.root / "_assets" / kept.json()["page_id"]
    suffixes = sorted(p.suffix for p in assets.iterdir() if p.name != ".gitignore")
    assert suffixes == [".avif", ".gif", ".webp"]
    article = keep_env.page_text(kept.json()["path"])
    assert "![x](https://img.example/fake.png)" in article
    assert "![x](https://img.example/vector.svg)" in article


async def test_archive_links_images_from_where_the_page_is_when_it_is_written(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    kb = keep_env.kb
    moved: list[str] = []

    async def move_then_serve(request: httpx.Request) -> httpx.Response:
        if not moved:  # the author moves the page while the images download
            page_id = keep_env.state(post_id)[0]
            assert page_id is not None
            await kb.move(PageId(page_id), PurePosixPath("Reading/Deep"))
            moved.append(page_id)
        return httpx.Response(200, content=PNG_MAGIC + b"i", headers={"content-type": PNG})

    mock.get("https://img.example/a.png").mock(side_effect=move_then_serve)
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "![a](https://img.example/a.png)")
    await keep_env.keep(post_id)
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done" and moved
    path = kb.resolve(PageId(moved[0]))
    assert path is not None and path.parent == PurePosixPath("Reading/Deep")
    text = keep_env.page_text(str(path))
    link = re.search(r"\]\((\.\./\.\./_assets/[^)]+)\)", text)
    assert link is not None and (keep_env.root / path.parent / link.group(1)).resolve().is_file()


async def test_article_hashtags_do_not_become_page_tags(keep_env: KeepEnv) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "Big news #breaking today, see C# and #2026.")
    kept = await keep_env.keep(post_id, "My #mine note.", ["sqlite"])
    await keep_env.rss.keeper.drain()
    text = keep_env.page_text(kept.json()["path"])
    body = text.split("---\n", 2)[2]
    assert body.startswith("My #mine note.\n\nTags: #sqlite\n")
    assert "Big news \\#breaking today, see C# and \\#2026." in body
    assert extract_tags(body) == ["mine", "sqlite"]
    counts = keep_env.kb.index.tag_counts()
    assert "breaking" not in counts and "2026" not in counts and counts["mine"] == 1


async def test_archive_retry_is_refused_unless_the_archive_failed(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "Archived text.")
    kept = await keep_env.keep(post_id)
    # Still archiving: the running archive is not started again.
    running = await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
    assert running.status_code == 409 and running.json()["error"] == "ArchiveNotFailed"
    await keep_env.rss.keeper.drain()
    page = await keep_env.kb.read(PageId(kept.json()["page_id"]))
    edited = page.page.body.replace("Archived text.", "MY EDIT.")
    await keep_env.kb.write(
        PageDraft(page.page.id, page.page.title, page.page.path.parent, edited, page.page.jesinia),
        expected_rev=page.rev,
    )
    done = await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
    assert done.status_code == 409 and done.json()["error"] == "ArchiveNotFailed"
    await keep_env.rss.keeper.drain()
    assert "MY EDIT." in keep_env.page_text(kept.json()["path"])
    assert keep_env.state(post_id)[1] == "done"


async def test_a_crash_after_the_page_write_still_finishes_the_archive(
    cfg: Config, kb_root: Path, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with keep_server(cfg, kb_root) as env:
        post_id = env.post()
        env.cache_article(post_id, "The full article.")
        keeper = env.rss.keeper

        def crash(*args: object) -> None:
            raise SystemExit("the server died right after writing the page")

        monkeypatch.setattr(keeper._store, "set_kept", crash)
        with pytest.raises(SystemExit):
            await keeper.keep(post_id, "Mine.", [], None)
        monkeypatch.undo()
        # The page is on disk and the post already says it is archiving.
        assert env.state(post_id) == (None, "archiving", None)
        page_files = list((kb_root / "Sources").iterdir())
        assert len(page_files) == 1
    async with keep_server(cfg, kb_root) as again:
        await again.rss.keeper.drain()
        page_id, state, _ = again.state(post_id)
        assert state == "done" and page_id is not None
        assert again.kb.resolve(PageId(page_id)) == PurePosixPath("Sources") / page_files[0].name
        assert "The full article." in page_files[0].read_text()


async def test_a_failed_keep_again_leaves_the_failed_archive_as_it_was(
    keep_env: KeepEnv, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock.get("https://fly.io/blog/sqlite-wal").mock(return_value=httpx.Response(500))
    post_id = keep_env.post()
    await keep_env.keep(post_id, "First.")
    await keep_env.rss.keeper.drain()
    failed = keep_env.state(post_id)
    assert failed[1] == "failed"
    kb = keep_env.kb

    async def stale(draft: PageDraft, *, expected_rev: str | None = None) -> None:
        raise StaleWrite("changed since read")

    monkeypatch.setattr(kb, "write", stale)
    refused = await keep_env.keep(post_id, "Again.")
    assert refused.status_code == 409 and refused.json()["error"] == "StaleWrite"
    assert keep_env.state(post_id) == failed
    assert archive_state("failed") == "failed" and archive_state("bogus") is None


async def test_a_crash_before_the_page_write_leaves_nothing_to_resume(
    cfg: Config, kb_root: Path, mock: respx.MockRouter
) -> None:
    async with keep_server(cfg, kb_root) as env:
        post_id = env.post()
        env.conn.execute("UPDATE posts SET archive_state = 'archiving' WHERE id = ?", (post_id,))
    async with keep_server(cfg, kb_root) as again:
        await again.rss.keeper.drain()
        assert again.state(post_id) == (None, None, None)


async def test_archive_failure_is_visible_and_retryable(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    route = mock.get("https://fly.io/blog/sqlite-wal").mock(return_value=httpx.Response(503))
    post_id = keep_env.post()
    kept = await keep_env.keep(post_id, "Keep it anyway.")
    assert kept.status_code == 201
    await keep_env.rss.keeper.drain()
    page_id, state, error = keep_env.state(post_id)
    assert (page_id, state) == (kept.json()["page_id"], "failed")
    assert error is not None and "503" in error
    text = keep_env.page_text(kept.json()["path"])
    assert FEED_FALLBACK_NOTE in text and "The feed's own summary of the post." in text

    route.mock(
        return_value=httpx.Response(
            200, content=ARTICLE_FIXTURE.read_bytes(), headers={"content-type": "text/html"}
        )
    )
    mock.get(url__regex=r"https://(news|cdn)\.example(\.org)?/.*\.png").mock(
        return_value=httpx.Response(200, content=PNG_MAGIC + b"img", headers={"content-type": PNG})
    )
    retry = await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
    assert retry.status_code == 202, retry.text
    assert retry.json()["archive_state"] == "archiving"
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id) == (page_id, "done", None)
    text = keep_env.page_text(kept.json()["path"])
    assert "Keep it anyway." in text and FEED_FALLBACK_NOTE not in text
    assert "Most personal tools never outgrow a single file database" in text
    assert "](../_assets/" in text


def _post_changes(env: KeepEnv) -> list[dict[str, object]]:
    """Collects ``rss.post_changed`` payloads from the app's event bus."""
    seen: list[dict[str, object]] = []

    async def record(event: Event) -> None:
        seen.append(dict(event.payload))

    env.app.state.ctx.bus.subscribe("rss.post_changed", record)
    return seen


async def test_archive_state_changes_are_published(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    seen = _post_changes(keep_env)
    route = mock.get("https://fly.io/blog/sqlite-wal").mock(return_value=httpx.Response(503))
    post_id = keep_env.post()
    kept = await keep_env.keep(post_id, "Keep it.")
    assert kept.status_code == 201
    await keep_env.rss.keeper.drain()
    assert seen[0] == {"id": post_id, "archive_state": "archiving", "archive_error": None}
    assert seen[-1]["id"] == post_id and seen[-1]["archive_state"] == "failed"
    assert "503" in str(seen[-1]["archive_error"])

    seen.clear()
    route.mock(
        return_value=httpx.Response(
            200, content=ARTICLE_FIXTURE.read_bytes(), headers={"content-type": "text/html"}
        )
    )
    mock.get(url__regex=r"https://(news|cdn)\.example(\.org)?/.*\.png").mock(
        return_value=httpx.Response(200, content=PNG_MAGIC + b"img", headers={"content-type": PNG})
    )
    retry = await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
    assert retry.status_code == 202
    await keep_env.rss.keeper.drain()
    assert seen[0]["archive_state"] == "archiving"
    assert seen[-1] == {"id": post_id, "archive_state": "done", "archive_error": None}


async def test_every_post_of_a_kept_page_hears_its_archive_end(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    seen = _post_changes(keep_env)
    mock.get("https://fly.io/blog/sqlite-wal").mock(return_value=httpx.Response(503))
    first = keep_env.post()
    second = keep_env.post(feed="https://other.example/feed.xml")
    await keep_env.keep(first, "One.")
    await keep_env.rss.keeper.drain()
    await keep_env.keep(second, "Two.")  # same source: the failed archive runs again
    await keep_env.rss.keeper.drain()
    ends = {(p["id"], p["archive_state"]) for p in seen[-2:]}
    assert ends == {(first, "failed"), (second, "failed")}


async def test_archive_retries_once_against_a_page_edited_meanwhile(
    keep_env: KeepEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "Archived text.")
    kb = keep_env.kb
    original = kb.write
    edits: list[str] = []

    async def edited_meanwhile(draft: PageDraft, *, expected_rev: str | None = None):  # type: ignore[no-untyped-def]
        if draft.id is not None and not edits:
            # The author saves the page in an editor just before the archive's write.
            path = keep_env.root / str(kb.resolve(draft.id))
            path.write_text(path.read_text().replace("Mine.", "Mine, edited."))
            edits.append(str(path))
        return await original(draft, expected_rev=expected_rev)

    monkeypatch.setattr(kb, "write", edited_meanwhile)
    kept = await keep_env.keep(post_id, "Mine.")
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done" and edits
    text = keep_env.page_text(kept.json()["path"])
    assert "Mine, edited." in text and "Archived text." in text


async def test_archive_gives_up_after_a_second_conflict(
    keep_env: KeepEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "Archived text.")
    kb = keep_env.kb
    original = kb.write

    async def always_stale(draft: PageDraft, *, expected_rev: str | None = None):  # type: ignore[no-untyped-def]
        if draft.id is not None:
            raise StaleWrite(f"{draft.id}: changed since read")
        return await original(draft, expected_rev=expected_rev)

    monkeypatch.setattr(kb, "write", always_stale)
    await keep_env.keep(post_id, "Mine.")
    await keep_env.rss.keeper.drain()
    _, state, error = keep_env.state(post_id)
    assert state == "failed" and error is not None and "changed since read" in error


async def test_stranded_archiving_posts_resume_on_start(
    cfg: Config, kb_root: Path, mock: respx.MockRouter
) -> None:
    async with keep_server(cfg, kb_root) as env:
        post_id = env.post()
        env.cache_article(post_id, "Archived after the restart.")
        kept = await env.keep(post_id, "Mine.")
        await env.rss.keeper.drain()
        # The server stopped mid-archive: the state says archiving, the page has feed text.
        page = await env.kb.read(PageId(kept.json()["page_id"]))
        stale = replace_article(page.page.body, "Feed text only.")
        await env.kb.write(
            PageDraft(
                page.page.id, page.page.title, page.page.path.parent, stale, page.page.jesinia
            ),
            expected_rev=page.rev,
        )
        env.conn.execute("UPDATE posts SET archive_state = 'archiving' WHERE id = ?", (post_id,))
    async with keep_server(cfg, kb_root) as again:
        await again.rss.keeper.drain()
        assert again.state(post_id)[1] == "done"
        assert "Archived after the restart." in again.page_text(kept.json()["path"])


async def test_module_without_kb_or_agents_reports_keep_unavailable(cfg: Config) -> None:
    rss = RssModule()
    app = create_app(cfg, [StubFetchModule(), rss], static_root=None)
    async with running(app):
        with pytest.raises(Exception, match="requires the kb service"):
            rss.keeper  # noqa: B018 - the property raises without kb
        with pytest.raises(Exception, match="requires the agents service"):
            rss.engage  # noqa: B018 - the property raises without agents


async def test_module_settings_need_a_started_module() -> None:
    with pytest.raises(HTTPException, match="not started"):
        RssModule().settings()
    with pytest.raises(HTTPException, match="not started"):
        RssModule().keeper  # noqa: B018 - the property raises when stopped
    with pytest.raises(HTTPException, match="not started"):
        RssModule().engage  # noqa: B018 - the property raises when stopped


# --- settings -------------------------------------------------------------------------------


async def test_settings_round_trip_and_apply_live(keep_env: KeepEnv) -> None:
    got = await keep_env.client.get(f"{KEEP_API}/settings")
    assert got.status_code == 200, got.text
    assert got.json() == {"retention": 30, "source_folder": "Sources"}

    put = await keep_env.client.put(
        f"{KEEP_API}/settings", json={"retention": 7, "source_folder": "Reading/Kept"}
    )
    assert put.status_code == 200, put.text
    assert put.json() == {"retention": 7, "source_folder": "Reading/Kept"}
    assert (await keep_env.client.get(f"{KEEP_API}/settings")).json() == put.json()
    saved = tomllib.loads((keep_env.cfg.data_dir / "config.toml").read_text())
    assert saved["rss"]["retention"] == "7d"
    assert saved["folders"]["source"] == "Reading/Kept"

    # Live: the next Keep uses the new folder, the next prune the new window.
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "x")
    kept = await keep_env.keep(post_id)
    assert kept.json()["path"].startswith("Reading/Kept/")
    old = keep_env.post(url="https://example.org/old")
    stale = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    keep_env.conn.execute("UPDATE posts SET published_at = ? WHERE id = ?", (stale, old))
    await keep_env.rss.prune_posts()
    assert keep_env.conn.execute("SELECT COUNT(*) FROM posts WHERE id = ?", (old,)).fetchone() == (
        0,
    )

    for bad in ({"retention": 0, "source_folder": "S"}, {"retention": 366, "source_folder": "S"}):
        assert (await keep_env.client.put(f"{KEEP_API}/settings", json=bad)).status_code == 422
    for folder in ("../outside", "/abs", "Journal", "_assets/x", ".hidden", ""):
        refused = await keep_env.client.put(
            f"{KEEP_API}/settings", json={"retention": 7, "source_folder": folder}
        )
        assert refused.status_code in (400, 422), folder
        if refused.status_code == 400:
            assert refused.json()["error"] == "InvalidSettings"
    unkeyed = await keep_env.client.put(
        f"{KEEP_API}/settings",
        json={"retention": 7, "source_folder": "S"},
        headers={"Idempotency-Key": ""},
    )
    assert unkeyed.status_code == 400
    assert (await keep_env.client.get(f"{KEEP_API}/settings")).json() == put.json()


async def test_settings_report_an_unreadable_config_file(keep_env: KeepEnv) -> None:
    (keep_env.cfg.data_dir / "config.toml").write_text("this is = = not toml")
    refused = await keep_env.client.put(
        f"{KEEP_API}/settings", json={"retention": 7, "source_folder": "Sources"}
    )
    assert refused.status_code == 400 and refused.json()["error"] == "InvalidSettings"
    assert "config.toml" in refused.json()["detail"]
    assert keep_env.app.state.ctx.cfg.rss_retention == timedelta(days=30)


# --- pure helpers ---------------------------------------------------------------------------


def test_kept_body_and_keep_again_without_a_tags_line() -> None:
    body = render_kept_body("", [], None, "Text.")
    assert body == "## Article\n\nText.\n"
    again = kept_again(body, "Later.", date(2026, 10, 17), ["new"], None)
    assert again == "_Kept again 2026-10-17:_ Later.\n\nTags: #new\n\n## Article\n\nText.\n"
    # An empty note adds no dated line; known tags (any case) are not repeated.
    assert kept_again(again, "  ", date(2026, 10, 18), ["NEW"], None) == again


def test_keep_again_puts_a_new_takeaway_after_the_old_ones() -> None:
    first = render_kept_body(
        "Note.", ["a"], "<!-- jesinia:ai x -->\nOld\n<!-- /jesinia:ai -->", "T"
    )
    again = kept_again(
        first, "", date(2026, 10, 17), [], "<!-- jesinia:ai y -->\nNew\n<!-- /jesinia:ai -->"
    )
    head = again.split("## Article", 1)[0]
    assert head.index("Old") < head.index("New") < len(head)
    assert again.endswith("## Article\n\nT\n")


def test_replace_article_appends_a_section_when_the_heading_is_gone() -> None:
    assert replace_article("Just a note.\n", "Body") == "Just a note.\n\n## Article\n\nBody\n"
    assert replace_article("N\n\n## Article\n\nOld\n", "New") == "N\n\n## Article\n\nNew\n"


def test_find_and_rewrite_images() -> None:
    markdown = (
        "![a](https://x.example/a.png) and ![b \\] c](<https://x.example/b c.png>) "
        "and [link](https://x.example/a.png) and ![](https://x.example/a.png)"
    )
    found = find_images(markdown)
    assert [ref.url for ref in found] == [
        "https://x.example/a.png",
        "https://x.example/b c.png",
        "https://x.example/a.png",
    ]
    rewritten = rewrite_images(markdown, {"https://x.example/a.png": "../_assets/p/1.png"})
    assert rewritten == (
        "![a](../_assets/p/1.png) and ![b \\] c](<https://x.example/b c.png>) "
        "and [link](https://x.example/a.png) and ![](../_assets/p/1.png)"
    )
    escaped = find_images("![x](<https://x.example/\\<y\\>.png>)")
    assert [ref.url for ref in escaped] == ["https://x.example/<y>.png"]


def test_front_matter_leaves_out_what_the_feed_did_not_give() -> None:
    post = PostView(
        id=1, feed_id=1, feed_title="F", url="https://a.example/p", title="T", author=None,
        published_at=None, first_seen_at=PUBLISHED, snippet="", content_html=None,
        discussion_url=None, is_summary=False, read=False, kept_page_id=None,
        archive_state=None, archive_error=None, has_full_article=False,
    )  # fmt: skip
    front = kept_front_matter(post, "https://a.example/feed", PUBLISHED)
    assert list(front) == ["kind", "source", "feed", "captured"]
    assert front["captured"] == "2026-10-14T09:30:00+00:00"
    assert kept_title(replace_config(post, title="  A \n split   title ")) == "A split title"
    assert kept_title(replace_config(post, title="   ")) == "https://a.example/p"


def test_is_http_url() -> None:
    assert is_http_url("https://a.example/x") and is_http_url("http://a.example")
    for bad in ("ftp://a.example/x", "javascript:alert(1)", "https:///nohost", "http://[::1"):
        assert not is_http_url(bad)


def test_settings_validation() -> None:
    assert validate_settings(30, " Reading/Kept/ ").source_folder == "Reading/Kept"
    for days in (0, 366):
        with pytest.raises(InvalidSettings, match="1 to 365 days"):
            validate_settings(days, "Sources")
    for folder in ("", "/", "/abs", "../x", "a/../b", "drafts", "x/_assets", "x/.hidden"):
        with pytest.raises(InvalidSettings):
            validate_settings(30, folder)


def test_a_note_line_reading_like_the_article_heading_does_not_split_the_page() -> None:
    note = "Mine.\n## Article\nStill my note."
    body = render_kept_body(note, ["a"], None, "Feed text.")
    assert body.count("\n## Article\n") == 1 and "\\## Article\nStill my note." in body
    archived = replace_article(body, "Full text.")
    assert archived.startswith("Mine.\n\\## Article\nStill my note.\n\nTags: #a\n\n")
    assert archived.endswith("## Article\n\nFull text.\n")
    again = kept_again(archived, "More.\n## Article", date(2026, 10, 17), ["b"], None)
    assert again.count("\n## Article\n") == 1
    assert "_Kept again 2026-10-17:_ More.\n\\## Article\n\nTags: #a #b\n\n## Article" in again


async def test_archive_logs_name_the_host_never_the_path(
    keep_env: KeepEnv, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock.get(url__regex=r"https://fly\.io/.*").mock(return_value=httpx.Response(503))
    mock.get(url__regex=r"https://img\.example/.*").mock(return_value=httpx.Response(404))
    post_id = keep_env.post(url="https://fly.io/private/token-abc123?key=s3cret")
    keep_env.cache_article(post_id, "![x](https://img.example/secret-path/a.png?sig=s3cret)")
    with caplog.at_level(logging.DEBUG):
        await keep_env.keep(post_id)
        await keep_env.rss.keeper.drain()
        keep_env.conn.execute("UPDATE posts SET full_md = NULL WHERE id = ?", (post_id,))
        keep_env.conn.execute("UPDATE posts SET archive_state = 'failed' WHERE id = ?", (post_id,))
        await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
        await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "failed"
    logged = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("jesinia"))
    assert "img.example" in logged and "fly.io" in logged
    for secret in ("token-abc123", "s3cret", "secret-path", "/private"):
        assert secret not in logged, logged


async def test_archives_run_a_few_at_a_time_and_are_forgotten_when_done(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    running = 0
    peak = 0
    full = asyncio.Event()  # as many archives as may run are inside the gate
    release = asyncio.Event()  # the test lets them finish

    async def gated(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == ARCHIVE_CONCURRENCY:
            full.set()
        await release.wait()
        running -= 1
        return httpx.Response(503)

    mock.get(url__regex=r"https://many\.example/.*").mock(side_effect=gated)
    keeper = keep_env.rss.keeper
    for n in range(6):
        await keeper.keep(keep_env.post(url=f"https://many.example/{n}"), "", [], None)
    # No archive can finish before the gate opens, so all six are pending.
    assert len(keeper.pending()) == 6
    await asyncio.wait_for(full.wait(), timeout=5)
    for _ in range(50):  # every other archive gets the chance to start, and must not
        await asyncio.sleep(0)
    assert running == peak == ARCHIVE_CONCURRENCY < 6
    release.set()
    await asyncio.wait_for(keeper.drain(), timeout=5)
    assert peak == ARCHIVE_CONCURRENCY
    assert keeper.pending() == [] and keeper._archives == {}  # nothing kept per page


async def test_deleting_a_feed_keeps_its_kept_posts_and_their_archive(
    keep_env: KeepEnv, mock: respx.MockRouter
) -> None:
    release = asyncio.Event()

    async def gated(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(503)

    mock.get("https://fly.io/blog/sqlite-wal").mock(side_effect=gated)
    post_id = keep_env.post()
    other = keep_env.post(url="https://fly.io/blog/other")  # not kept: goes with the feed
    feed_id = keep_env.conn.execute(
        "SELECT feed_id FROM posts WHERE id = ?", (post_id,)
    ).fetchone()[0]
    kept = await keep_env.keep(post_id, "Mine.")
    assert keep_env.state(post_id)[1] == "archiving"
    assert (await keep_env.client.delete(f"{KEEP_API}/feeds/{feed_id}")).status_code == 204
    release.set()
    await keep_env.rss.keeper.drain()
    # The archive still records its outcome on the post, and Retry is still offered.
    page_id, state, error = keep_env.state(post_id)
    assert (page_id, state) == (kept.json()["page_id"], "failed") and error
    listed = (await keep_env.client.get(f"{KEEP_API}/posts", params={"view": "kept"})).json()
    assert [(p["id"], p["feed_title"]) for p in listed["items"]] == [
        (post_id, "Feed https://fly.io/blog/feed.xml")
    ]
    unread = (await keep_env.client.get(f"{KEEP_API}/posts", params={"view": "unread"})).json()
    assert unread["items"] == []
    assert keep_env.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE id = ?", (other,)
    ).fetchone() == (0,)
    mock.get("https://fly.io/blog/sqlite-wal").mock(
        return_value=httpx.Response(
            200, content=_html("<p>Back at last.</p>"), headers={"content-type": "text/html"}
        )
    )
    retry = await keep_env.client.post(f"{KEEP_API}/posts/{post_id}/archive")
    assert retry.status_code == 202, retry.text
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id)[1] == "done"


async def test_a_prune_during_keep_never_takes_the_post_being_kept(
    keep_env: KeepEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_id = keep_env.post()
    keep_env.cache_article(post_id, "The article.")
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    keep_env.conn.execute("UPDATE posts SET published_at = ? WHERE id = ?", (old, post_id))
    kb = keep_env.kb
    original = kb.write
    pruned: list[int] = []

    async def prune_first(draft: PageDraft, *, expected_rev: str | None = None):  # type: ignore[no-untyped-def]
        if draft.id is None and not pruned:
            # The daily prune runs after Keep marked the post, before its page is written.
            assert keep_env.state(post_id) == (None, "archiving", None)
            pruned.append(keep_env.rss.posts.prune(datetime.now(UTC)))
        return await original(draft, expected_rev=expected_rev)

    monkeypatch.setattr(kb, "write", prune_first)
    kept = await keep_env.keep(post_id, "Mine.")
    assert kept.status_code == 201, kept.text
    assert pruned == [0]
    tombstones = keep_env.conn.execute("SELECT COUNT(*) FROM pruned_posts").fetchone()[0]
    assert tombstones == 0
    await keep_env.rss.keeper.drain()
    assert keep_env.state(post_id) == (kept.json()["page_id"], "done", None)
