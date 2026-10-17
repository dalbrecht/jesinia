# SPDX-License-Identifier: AGPL-3.0-or-later
import html
import re
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
import trafilatura
from fastapi import APIRouter
from httpx import Response
from markdown_it import MarkdownIt

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.module import AppContext, ModuleHealth, ToolDef
from jesinia.services.fetch.application.ports import Article, CachedFetch
from jesinia.services.fetch.application.service import FetchError, FetchService
from jesinia.services.fetch.infrastructure.extract import TrafilaturaExtractor, _clean_tree
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


# Pages modelled on real blogs whose archives came out broken (headings that are anchor links,
# site clutter after the post).
JVNS_URL = "https://jvns.ca/blog/2026/09/27/replacing-the-old-battery-on-rechargeable-bike-lights/"
WILLISON_URL = "https://simonwillison.net/2026/Oct/10/local-models/"
FIXTURE_URLS = {
    "article.html": ARTICLE_URL,
    "jvns-anchor-headings.html": JVNS_URL,
    "willison-recent-articles.html": WILLISON_URL,
    "willison-highlighted-pre.html": "https://simonwillison.net/2026/Oct/12/python-3-15/",
}


def _extract_fixture(name: str) -> Article:
    article = TrafilaturaExtractor().extract((FIXTURES / name).read_bytes(), FIXTURE_URLS[name])
    assert article is not None
    return article


def test_extract_unwraps_anchor_links_in_headings() -> None:
    markdown = _extract_fixture("jvns-anchor-headings.html").markdown
    lines = markdown.splitlines()
    assert "### step 1: cut it open" in lines, markdown
    assert "### step 2: find the battery model" in lines, markdown
    assert "## what I learned" in lines, markdown
    assert "#step-1" not in markdown and "#what-i-learned" not in markdown


def test_extract_keeps_code_blocks_images_and_body_of_an_anchor_heading_page() -> None:
    article = _extract_fixture("jvns-anchor-headings.html")
    markdown = article.markdown
    assert "```\n$ multimeter --dc\n3.71 V\n```" in markdown, markdown
    assert "    def solder(wire_a, wire_b):\n        return join(wire_a, wire_b)" in markdown
    assert "![the bike light cut open](https://jvns.ca/images/bike-light-open.jpg)" in markdown
    assert article.image_urls == ["https://jvns.ca/images/bike-light-open.jpg"]
    assert "My bike lights stopped holding a charge this summer." in markdown
    assert "\nNext time I will buy lights with a removable battery" in markdown, markdown
    for clutter in ("Categories", "Subscribe to the newsletter", "feed.xml"):
        assert clutter not in markdown, markdown


def test_extract_drops_recent_articles_navigation_and_sidebars() -> None:
    article = _extract_fixture("willison-recent-articles.html")
    markdown = article.markdown
    for clutter in ("Recent articles", "Five SQLite tricks", "/tags/", "Elsewhere", "Colophon"):
        assert clutter not in markdown, markdown
    assert "## Notes on running local models with a long context" in markdown
    assert "[KV cache](https://example.org/kv-cache)" in markdown
    assert "I expect this to keep improving quickly" in markdown
    assert "llm -m local-model --option context 131072 'summarize this'\n```" in markdown
    assert article.image_urls == ["https://static.simonwillison.net/static/2026/memory-chart.png"]


def test_extract_keeps_an_article_whose_own_class_looks_like_clutter() -> None:
    page = (
        '<html><body><article class="post recent">'
        f"<h1>Recent thoughts</h1>{_FILLER}{_FILLER}"
        '<div class="related-posts"><p>Related: another post you might like.</p></div>'
        '<div aria-label="Share this post"><p>Share on the social network.</p></div>'
        "<aside><p>An aside the site uses for promotions.</p></aside>"
        f"{_FILLER}</article></body></html>"
    )
    article = TrafilaturaExtractor().extract(page.encode(), ARTICLE_URL)
    assert article is not None
    assert "An article long enough for the extractor" in article.markdown
    for clutter in ("another post you might like", "social network", "promotions"):
        assert clutter not in article.markdown, article.markdown


def _is_only_a_link(text: str) -> bool:
    tokens = MarkdownIt("commonmark").parse(text.strip())
    children = [c.type for t in tokens for c in (t.children or []) if c.type != "text"]
    return len(tokens) == 3 and bool(children) and children[0] in ("link_open", "image")


@pytest.mark.parametrize("name", sorted(FIXTURE_URLS))
def test_extracted_markdown_has_no_empty_heading_or_inline_code_block(name: str) -> None:
    markdown = _extract_fixture(name).markdown
    tokens = MarkdownIt("commonmark").parse(markdown)
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            assert tokens[index + 1].content.strip(), markdown
        if token.type == "code_block":
            assert not _is_only_a_link(token.content), markdown
    # Every <pre> in the fixtures comes out fenced: an indented block is misplaced prose.
    assert [t.content for t in tokens if t.type == "code_block"] == [], markdown


def test_extract_flattens_headings_with_inline_children_and_drops_empty_ones() -> None:
    page = (
        f"<html><body><article><h1>Tools</h1>{_FILLER}"
        '<h2><a href="#log"><code>git\n   log</code>  for\n     history\n</a></h2>'
        f"{_FILLER}"
        '<h3><a href="#nothing"></a></h3>'
        f"{_FILLER}</article></body></html>"
    )
    article = TrafilaturaExtractor().extract(page.encode(), ARTICLE_URL)
    assert article is not None
    assert "## `git log` for history" in article.markdown.splitlines(), article.markdown
    assert "#log" not in article.markdown and "#nothing" not in article.markdown
    tokens = MarkdownIt("commonmark").parse(article.markdown)
    headings = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "heading_open"]
    assert headings == ["Tools", "`git log` for history"], article.markdown


CLUTTER_NAMES = {
    "post-comments": True,
    "share-buttons": True,
    "site-nav": True,
    "recentArticles": True,
    "commentary": False,
    "shared-content": False,
    "navy": False,
    "canvas": False,
}


def _clutter_page(attribute: str, name: str) -> bytes:
    return (
        f"<html><body><article><h1>Words</h1>{_FILLER}"
        f'<div {attribute}="{name}"><p>Marker sentence for the {name} block.</p></div>'
        f"{_FILLER}</article></body></html>"
    ).encode()


@pytest.mark.parametrize(("name", "is_clutter"), CLUTTER_NAMES.items(), ids=CLUTTER_NAMES.keys())
def test_clean_tree_matches_clutter_names_as_whole_words(name: str, is_clutter: bool) -> None:
    for attribute in ("class", "id", "aria-label"):
        raw = trafilatura.load_html(_clutter_page(attribute, name))
        kept = (
            f"Marker sentence for the {name} block." in _clean_tree(raw, ARTICLE_URL).text_content()
        )
        assert kept is not is_clutter, attribute


@pytest.mark.parametrize(("name", "is_clutter"), CLUTTER_NAMES.items(), ids=CLUTTER_NAMES.keys())
def test_extract_keeps_or_drops_labelled_blocks_by_whole_words(name: str, is_clutter: bool) -> None:
    # aria-label only: trafilatura applies its own substring rules to class and id.
    article = TrafilaturaExtractor().extract(_clutter_page("aria-label", name), ARTICLE_URL)
    assert article is not None
    kept = f"Marker sentence for the {name} block." in article.markdown
    assert kept is not is_clutter, article.markdown


def test_extract_keeps_markdown_shown_inside_a_code_block() -> None:
    sample = "```\n    ```\n### \nnot a heading\n```"
    page = (
        f"<html><body><article><h1>Fences</h1>{_FILLER}"
        f"<pre><code>{html.escape(sample)}\n</code></pre>{_FILLER}</article></body></html>"
    )
    article = TrafilaturaExtractor().extract(page.encode(), ARTICLE_URL)
    assert article is not None
    tokens = MarkdownIt("commonmark").parse(article.markdown)
    assert [t.content for t in tokens if t.type == "fence"] == [sample + "\n"], article.markdown


def test_extract_keeps_highlighted_pre_blocks_without_code_as_fenced_code() -> None:
    markdown = _extract_fixture("willison-highlighted-pre.html").markdown
    tokens = MarkdownIt("commonmark").parse(markdown)
    fences = [t.content for t in tokens if t.type == "fence"]
    assert fences == [
        "lists = [[1, 2], [3, 4]]\n"
        "[*L for L in lists]\n"
        "# [1, 2, 3, 4]\n"
        "\n"
        "# equivalent to [x for L in lists for x in L]\n"
        "    indented =  True\n",
        "# install it\nuv python install 3.15\n\n# then run it\nuv run --python 3.15 python\n",
    ], markdown
    headings = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "heading_open"]
    assert headings == ["Hello Python 3.15", "Unpacking in comprehensions"], markdown
    assert "Both examples run unchanged on the release candidate" in markdown


def _fences_of(body: str) -> tuple[list[str], str]:
    page = f"<html><body><article><h1>Code</h1>{_FILLER}{body}{_FILLER}</article></body></html>"
    article = TrafilaturaExtractor().extract(page.encode(), ARTICLE_URL)
    assert article is not None
    tokens = MarkdownIt("commonmark").parse(article.markdown)
    return [t.content for t in tokens if t.type == "fence"], article.markdown


def test_extract_keeps_line_breaks_and_blank_lines_in_code() -> None:
    fences, markdown = _fences_of("<pre>one<br>two<br><br># three</pre>")
    assert fences == ["one\ntwo\n\n# three\n"], markdown
    highlighted = (
        '<pre><code><span class="k">a</span> = 1\n\n<span class="c"># b</span></code></pre>'
    )
    fences, markdown = _fences_of(highlighted)
    assert fences == ["a = 1\n\n# b\n"], markdown


def test_extract_leaves_a_page_that_already_uses_the_blank_line_symbol_alone() -> None:
    fences, markdown = _fences_of("<p>The ␀ symbol stands for NUL.</p><pre>a\n\nb</pre>")
    assert "The ␀ symbol stands for NUL." in markdown
    assert fences == ["a\nb\n"], markdown  # trafilatura's own handling of the blank line
