# SPDX-License-Identifier: AGPL-3.0-or-later
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from fastapi import APIRouter
from httpx import Response

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.fetch.application.ports import Article, CachedFetch
from jesinia.services.fetch.application.service import FetchError, FetchService
from jesinia.services.fetch.infrastructure.extract import TrafilaturaExtractor
from jesinia.services.fetch.module import FetchModule
from tests.conftest import lifespan_client

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE_URL = "https://news.example/2026/sqlite"
PUBLIC = "93.184.216.34"


class _NoCache:
    def get(self, url: str) -> CachedFetch | None:
        return None

    def put(self, url: str, entry: CachedFetch) -> None:
        return None

    def prune(self, older_than: timedelta) -> int:
        return 0


async def _public(host: str) -> list[str]:
    return [PUBLIC]


class _FixedExtractor:
    def __init__(self, article: Article | None) -> None:
        self.article = article
        self.calls: list[tuple[bytes, str]] = []

    def extract(self, html: bytes, url: str) -> Article | None:
        self.calls.append((html, url))
        return self.article


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_extract_article_returns_markdown_and_images() -> None:
    html = (FIXTURES / "article.html").read_bytes()
    article = TrafilaturaExtractor().extract(html, ARTICLE_URL)
    assert article is not None
    assert article.title == "Why SQLite Is Enough"
    assert "Most personal tools never outgrow a single file database" in article.markdown
    # Links and images are made absolute against the page URL.
    assert "[write-ahead log](https://news.example/docs/wal)" in article.markdown
    assert "![WAL diagram](https://news.example/img/wal-diagram.png)" in article.markdown
    assert article.image_urls == [
        "https://news.example/img/wal-diagram.png",
        "https://cdn.example.org/sqlite-logo.png",
    ]
    # Navigation and footer are not part of the article.
    assert "Copyright Example News" not in article.markdown


def test_extract_keeps_link_text_but_drops_non_http_targets() -> None:
    html = (FIXTURES / "article.html").read_bytes()
    article = TrafilaturaExtractor().extract(html, ARTICLE_URL)
    assert article is not None
    assert "javascript:" not in article.markdown
    assert "A sneaky link sits next to the closing remarks" in article.markdown


def test_extract_drops_non_http_images_and_keeps_plain_targets() -> None:
    html = (
        b"<html><body><article><h1>Data images</h1>"
        b"<p>An article long enough for the extractor to keep it as the main text of the page, "
        b"with a few more words added so it clearly counts as prose and not navigation.</p>"
        b'<img src="data:image/png;base64,AAAA" alt="inline">'
        b"<p>A second paragraph that closes the article off with some more ordinary words in "
        b'it, and a <a href="mailto:a@example.org">mail link</a> too.</p>'
        b"</article></body></html>"
    )
    article = TrafilaturaExtractor().extract(html, ARTICLE_URL)
    assert article is not None
    assert article.image_urls == []
    assert "data:image" not in article.markdown and "mailto:" not in article.markdown
    assert "mail link" in article.markdown


def test_extract_returns_none_when_the_page_has_no_article_text() -> None:
    assert TrafilaturaExtractor().extract(b"<html><body></body></html>", ARTICLE_URL) is None
    assert TrafilaturaExtractor().extract(b"", ARTICLE_URL) is None


async def test_article_composes_the_guarded_get_and_the_extractor(
    mock: respx.MockRouter,
) -> None:
    mock.get(ARTICLE_URL).mock(return_value=Response(200, content=b"<html>page</html>"))
    expected = Article("T", "Body", [])
    extractor = _FixedExtractor(expected)
    fetch = FetchService(_NoCache(), resolve=_public, min_interval=0, extractor=extractor)
    assert await fetch.article(ARTICLE_URL) == expected
    assert extractor.calls == [(b"<html>page</html>", ARTICLE_URL)]


async def test_article_reports_an_http_error_status_as_a_fetch_error(
    mock: respx.MockRouter,
) -> None:
    mock.get(ARTICLE_URL).mock(return_value=Response(404))
    extractor = _FixedExtractor(Article("T", "Body", []))
    fetch = FetchService(_NoCache(), resolve=_public, min_interval=0, extractor=extractor)
    with pytest.raises(FetchError, match="HTTP 404"):
        await fetch.article(ARTICLE_URL)
    assert extractor.calls == []


async def test_article_without_an_extractor_is_a_fetch_error() -> None:
    fetch = FetchService(_NoCache(), resolve=_public, min_interval=0)
    with pytest.raises(FetchError, match="extraction is not available"):
        await fetch.article(ARTICLE_URL)


async def test_fetch_module_injects_the_trafilatura_extractor(
    cfg: Config, mock: respx.MockRouter
) -> None:
    html = (FIXTURES / "article.html").read_bytes()
    mock.get(ARTICLE_URL).mock(return_value=Response(200, content=html))

    async def resolve(host: str) -> list[str]:
        return [PUBLIC]

    captured: dict[str, object] = {}

    class _Probe:
        name = "probe"

        def router(self) -> APIRouter | None:
            return None

        def mcp_tools(self) -> Sequence[ToolDef]:
            return []

        async def start(self, ctx: AppContext) -> None:
            captured["fetch"] = ctx.services["fetch"]

        async def stop(self) -> None:
            return None

        def health(self) -> ModuleHealth:
            return ModuleHealth(True, "probe")

    async with lifespan_client(create_app(cfg, [FetchModule(resolve=resolve), _Probe()])):
        fetch = captured["fetch"]
        assert isinstance(fetch, FetchService)
        article = await fetch.article(ARTICLE_URL)
    assert article is not None and article.title == "Why SQLite Is Enough"
