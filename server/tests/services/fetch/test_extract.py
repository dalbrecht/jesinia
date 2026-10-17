# SPDX-License-Identifier: AGPL-3.0-or-later
import html
import re
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import APIRouter
from httpx import Response
from markdown_it import MarkdownIt

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


_FILLER = (
    "<p>An article long enough for the extractor to keep it as the main text of the page, "
    "with a few more words added so it clearly counts as prose and not navigation.</p>"
)
# Every way a remote page might smuggle a script or data target into the Markdown.
HOSTILE_BODIES = {
    "bracketed anchor text": '<p>See <a href="javascript:alert(5)">nested [br]acket</a> now.</p>',
    "image inside a script link": (
        '<p><a href="javascript:alert(6)"><img src="https://i.example/x.png" alt="x"></a></p>'
    ),
    "mixed-case spaced scheme": '<p>A <a href="  JaVaScRiPt:alert(7)">spaced</a> link.</p>',
    "autolink-looking anchor": (
        '<p>Go <a href="javascript:alert(8)">&lt;javascript:alert(8)&gt;</a> there.</p>'
    ),
    "reference-style anchor": (
        '<p><a href="javascript:alert(9)">[ref]</a></p><p>[ref]: <a href="data:x">t</a></p>'
    ),
    "raw script": "<p>Before</p><script>alert(10)</script><p>After</p>",
    "data image": '<p><img src="data:image/png;base64,AAAA" alt="inline"></p>',
    "data link": '<p>A <a href="data:text/html,hi">data link</a> here.</p>',
}
_TARGET = re.compile(r"\]\(\s*<?([^)>\s]*)")


@pytest.mark.parametrize("body", HOSTILE_BODIES.values(), ids=HOSTILE_BODIES.keys())
def test_extract_never_yields_a_non_http_target(body: str) -> None:
    html = f"<html><body><article><h1>Hostile</h1>{_FILLER}{body}{_FILLER}</article></body></html>"
    article = TrafilaturaExtractor().extract(html.encode(), ARTICLE_URL)
    assert article is not None
    targets = _TARGET.findall(article.markdown)
    assert all(t.startswith(("https://", "http://")) for t in targets), article.markdown
    for bad in ("](javascript", "(<javascript", "data:", "alert(10)"):
        assert bad not in article.markdown.lower(), article.markdown
    assert re.search(r"(?<!\\)<javascript:", article.markdown, re.IGNORECASE) is None
    assert all(u.startswith(("https://", "http://")) for u in article.image_urls)


# http(s) URLs carrying characters that end a Markdown link destination early. trafilatura
# writes them as escaped angle-bracket destinations; an upgrade that stopped doing so would
# let the URL's tail become a second link with any scheme.
BREAKOUT_URLS = {
    "link injection": "https://a.example/x)[x](javascript:alert(1)",
    "bracket paren": "https://a.example/x)](y",
    "angle brackets": "https://a.example/<x>/z",
    "space": "https://a.example/a b",
    "backslash before paren": "https://a.example/x\\)(javascript:alert(2)",
}


def _targets(markdown: str) -> list[str]:
    """Every link href and image src a CommonMark renderer would see in ``markdown``."""
    found: list[str] = []
    for block in MarkdownIt("commonmark").parse(markdown):
        stack = list(block.children or [])
        while stack:
            token = stack.pop()
            stack.extend(token.children or [])
            for name in ("href", "src"):
                value = token.attrGet(name)
                if value is not None:
                    found.append(str(value))
    return found


def test_extract_keeps_breakout_urls_inside_their_destinations() -> None:
    links = "".join(
        f'<p>Link {n}: <a href="{html.escape(u)}">anchor {n}</a> and an image '
        f'<img src="{html.escape(u)}.png" alt="pic {n}"> in a sentence of prose.</p>'
        for n, u in enumerate(BREAKOUT_URLS.values())
    )
    page = (
        f"<html><body><article><h1>Breakout</h1>{_FILLER}{links}{_FILLER}</article></body></html>"
    )
    article = TrafilaturaExtractor().extract(page.encode(), ARTICLE_URL)
    assert article is not None
    targets = _targets(article.markdown)
    assert len(targets) == 2 * len(BREAKOUT_URLS), article.markdown  # every link and image
    assert all(t.startswith(("http://", "https://")) for t in targets), (targets, article.markdown)
    assert all(u.startswith(("http://", "https://")) for u in article.image_urls)


def test_extract_keeps_the_text_of_a_dropped_link_without_brackets() -> None:
    body = HOSTILE_BODIES["bracketed anchor text"]
    html = f"<html><body><article>{_FILLER}{body}{_FILLER}</article></body></html>"
    article = TrafilaturaExtractor().extract(html.encode(), ARTICLE_URL)
    assert article is not None
    # Plain text, not a ``[text]`` shortcut reference a renderer could resolve.
    assert "See nested \\[br\\]acket now." in article.markdown


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


async def test_article_extracts_against_the_url_after_redirects(
    mock: respx.MockRouter,
) -> None:
    mock.get(ARTICLE_URL).mock(return_value=Response(302, headers={"location": "/final"}))
    mock.get("https://news.example/final").mock(
        return_value=Response(
            200, content=b"<html>page</html>", headers={"content-type": "text/html"}
        )
    )
    extractor = _FixedExtractor(Article("T", "Body", []))
    fetch = FetchService(_NoCache(), resolve=_public, min_interval=0, extractor=extractor)
    await fetch.article(ARTICLE_URL)
    assert extractor.calls == [(b"<html>page</html>", "https://news.example/final")]


async def test_article_refuses_a_page_that_is_not_html(mock: respx.MockRouter) -> None:
    mock.get(ARTICLE_URL).mock(
        return_value=Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})
    )
    extractor = _FixedExtractor(Article("T", "Body", []))
    fetch = FetchService(_NoCache(), resolve=_public, min_interval=0, extractor=extractor)
    with pytest.raises(FetchError, match="not an HTML page"):
        await fetch.article(ARTICLE_URL)
    assert extractor.calls == []


async def test_article_wraps_a_transport_error_with_its_cause(mock: respx.MockRouter) -> None:
    mock.get(ARTICLE_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    fetch = FetchService(
        _NoCache(), resolve=_public, min_interval=0, extractor=_FixedExtractor(None)
    )
    with pytest.raises(FetchError, match="timed out") as caught:
        await fetch.article(ARTICLE_URL)
    assert isinstance(caught.value.__cause__, httpx.ConnectTimeout)


async def test_article_wraps_an_extractor_failure_with_its_cause(mock: respx.MockRouter) -> None:
    mock.get(ARTICLE_URL).mock(return_value=Response(200, content=b"<html>page</html>"))

    class _Broken:
        def extract(self, html: bytes, url: str) -> Article | None:
            raise ValueError("parser exploded")

    fetch = FetchService(_NoCache(), resolve=_public, min_interval=0, extractor=_Broken())
    with pytest.raises(FetchError, match="parser exploded") as caught:
        await fetch.article(ARTICLE_URL)
    assert isinstance(caught.value.__cause__, ValueError)


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


async def test_convert_extracts_given_html_without_fetching() -> None:
    extractor = _FixedExtractor(Article(None, "The text.", []))
    svc = FetchService(_NoCache(), resolve=_public, min_interval=0, extractor=extractor)
    converted = await svc.convert(b"<p>The text.</p>", "https://feed.example/post")
    assert converted == Article(None, "The text.", [])
    assert extractor.calls == [(b"<p>The text.</p>", "https://feed.example/post")]


async def test_convert_reports_failures_as_fetch_errors() -> None:
    with pytest.raises(FetchError, match="not available"):
        await FetchService(_NoCache(), resolve=_public).convert(b"<p>x</p>", ARTICLE_URL)

    class Broken:
        def extract(self, html: bytes, url: str) -> Article | None:
            raise ValueError("parser exploded")

    svc = FetchService(_NoCache(), resolve=_public, extractor=Broken())
    with pytest.raises(FetchError, match="parser exploded") as caught:
        await svc.convert(b"<p>x</p>", ARTICLE_URL)
    assert isinstance(caught.value.__cause__, ValueError)
