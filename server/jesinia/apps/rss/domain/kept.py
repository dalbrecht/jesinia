# SPDX-License-Identifier: AGPL-3.0-or-later
"""A kept post as a KB page (spec §3.3): front matter, body order, keeping again, the
``## Article`` section and its archived images."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from jesinia.apps.rss.domain.feed import RssError
from jesinia.apps.rss.domain.post import PostView
from jesinia.services.kb.domain.provenance import inert, provenance_label, wrap_ai
from jesinia.services.kb.domain.tags import (
    TAG_LINE_PREFIX,
    escape_tags,
    extract_tags,
    render_tag_line,
)

KIND = "source/rss"
ARTICLE_HEADING = "## Article"
FEED_FALLBACK_NOTE = "Archived from the feed; the full article wasn't available."
# Per kept page (spec §3.3, builder's call): at most 20 images, and at most 20 download
# attempts and 10 MB read in all, whether a download was kept or rejected.
ARCHIVE_MAX_IMAGES = 20
ARCHIVE_MAX_ATTEMPTS = ARCHIVE_MAX_IMAGES
ARCHIVE_MAX_BYTES = 10 * 1024 * 1024
ASSET_NAME_HEX = 32

ArchiveState = Literal["archiving", "done", "failed"]
_ARCHIVE_STATES: Mapping[str, ArchiveState] = {
    "archiving": "archiving",
    "done": "done",
    "failed": "failed",
}


def archive_state(raw: str | None) -> ArchiveState | None:
    """A stored archive state as its type (None for none, or for an unknown value)."""
    return _ARCHIVE_STATES.get(raw or "")


_ARTICLE = re.compile(rf"(?m)^{re.escape(ARTICLE_HEADING)}[ \t]*$")
_AI_OPEN = "<!-- jesinia:ai"
# ``![alt](destination "title")``: the destination is ``<…>`` (backslash escapes inside) or
# a bare run with at most one level of balanced parentheses.
_IMAGE = re.compile(
    r"!\[(?:\\.|[^\\\]\n])*\]\("
    r"(?P<dest><(?:\\.|[^\\<>\n])*>|(?:[^\s()<>\\]|\\.|\([^\s()<>]*\))+)"
    r"(?:[ \t]+\"[^\"\n]*\")?\)"
)
_ESCAPE = re.compile(r"\\(.)")


class PostNotKept(RssError):  # noqa: N818 - the error code clients see
    """The post has no kept page, so there is nothing to archive."""


class ArchiveNotFailed(RssError):  # noqa: N818 - the error code clients see
    """Retry is only for a failed archive: a done one (and the author's edits to it) is never
    overwritten, and a running one is not started twice."""


@dataclass(frozen=True)
class Takeaway:
    """The engaged agent's summary, as the author left it, for the page's AI section."""

    text: str
    agent: str
    model: str
    session: str


@dataclass(frozen=True)
class KeepResult:
    page_id: str
    path: PurePosixPath
    created: bool


@dataclass(frozen=True)
class ImageRef:
    """An image in Markdown: its destination's span in the text and the URL it names."""

    start: int
    end: int
    url: str


def kept_title(post: PostView) -> str:
    return " ".join(post.title.split()) or post.url


def kept_front_matter(post: PostView, feed_url: str, captured: datetime) -> dict[str, object]:
    """The page's ``jesinia:`` keys; ``published`` is a date (written bare, as in the spec)."""
    front: dict[str, object] = {"kind": KIND, "source": post.url, "feed": feed_url}
    if post.author:
        front["author"] = post.author
    if post.published_at is not None:
        front["published"] = post.published_at.astimezone(UTC).date()
    front["captured"] = captured.isoformat(timespec="seconds")
    if post.discussion_url:
        front["discussion"] = post.discussion_url
    return front


def takeaway_block(takeaway: Takeaway, at: datetime) -> str:
    """The takeaway inside provenance comments; nothing in it can open or close one."""
    return wrap_ai(
        f"Takeaway: {inert(' '.join(takeaway.text.split()))}",
        agent=provenance_label(takeaway.agent),
        model=provenance_label(takeaway.model),
        at=at,
        session=provenance_label(takeaway.session),
    )


def feed_article(markdown: str) -> str:
    """The feed's own content standing in for the article, saying so."""
    return f"_{FEED_FALLBACK_NOTE}_\n\n{markdown.strip()}".strip()


def _section(article: str) -> str:
    """The article section. Its hashtags are escaped: they are the source's, not the
    author's tags."""
    return f"{ARTICLE_HEADING}\n\n{escape_tags(inert(article.strip()))}\n"


def _note(text: str) -> str:
    """The author's text, unable to open a provenance block or start the article section."""
    return _ARTICLE.sub(lambda line: "\\" + line.group(0), inert(text.strip()))


def render_kept_body(note: str, tags: Sequence[str], ai_block: str | None, article: str) -> str:
    """Note, ``Tags:`` line, AI takeaway, then ``## Article`` (spec §3.3 order); empty
    parts are left out."""
    parts = [_note(note)] if note.strip() else []
    if tags:
        parts.append(render_tag_line(tags))
    if ai_block:
        parts.append(ai_block)
    parts.append(_section(article))
    return "\n\n".join(parts)


def _split_article(body: str) -> tuple[str, str]:
    found = _ARTICLE.search(body)
    return (body[: found.start()], body[found.start() :]) if found else (body, "")


def kept_again(body: str, note: str, on: date, tags: Sequence[str], ai_block: str | None) -> str:
    """``body`` after keeping its post again: the note appended under a dated line after
    the note block, new tags added to the ``Tags:`` line, a new takeaway after the old ones,
    and the article untouched."""
    head, article = _split_article(body)
    lines = head.split("\n")
    tag_at = next((i for i, line in enumerate(lines) if line.startswith(TAG_LINE_PREFIX)), None)
    ai_at = next((i for i, line in enumerate(lines) if line.startswith(_AI_OPEN)), None)
    cut = tag_at if tag_at is not None else ai_at if ai_at is not None else len(lines)
    known = set(extract_tags(lines[tag_at])) if tag_at is not None else set()
    added = [tag for tag in dict.fromkeys(t.lower() for t in tags) if tag not in known]
    if added and tag_at is not None:
        lines[tag_at] = " ".join([lines[tag_at].rstrip(), *(f"#{tag}" for tag in added)])
    note_part = "\n".join(lines[:cut]).strip("\n")
    rest = "\n".join(lines[cut:]).strip("\n")
    if added and tag_at is None:
        rest = "\n\n".join(p for p in (render_tag_line(added), rest) if p)
    if note.strip():
        dated = f"_Kept again {on.isoformat()}:_ {_note(note)}"
        note_part = "\n\n".join(p for p in (note_part, dated) if p)
    new_head = "\n\n".join(p for p in (note_part, rest, ai_block or "") if p)
    return f"{new_head}\n\n{article}" if article else f"{new_head}\n"


def replace_article(body: str, article: str) -> str:
    """``body`` with its ``## Article`` section replaced (appended if it was removed)."""
    head, found = _split_article(body)
    if not found:
        return "\n\n".join(p for p in (body.rstrip(), _section(article)) if p)
    return head + _section(article)


def find_images(markdown: str) -> list[ImageRef]:
    """Every inline image, in order (links that are not images are not included)."""
    refs: list[ImageRef] = []
    for match in _IMAGE.finditer(markdown):
        dest = match.group("dest")
        raw = dest[1:-1] if dest.startswith("<") else dest
        refs.append(ImageRef(match.start("dest"), match.end("dest"), _ESCAPE.sub(r"\1", raw)))
    return refs


def rewrite_images(markdown: str, local: Mapping[str, str]) -> str:
    """``markdown`` with each image whose URL is in ``local`` pointing at its local path."""
    out = markdown
    for ref in reversed(find_images(markdown)):
        if ref.url in local:
            out = out[: ref.start] + local[ref.url] + out[ref.end :]
    return out


def is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def image_extension(data: bytes) -> str | None:
    """The file extension for the raster image format ``data`` starts with, whatever a server
    claimed its type was; None for anything else (SVG included: it can carry script, and
    archived images are served from the KB)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return ".avif"
    return None


def asset_name(data: bytes) -> str | None:
    """A file name from the content's hash and an extension from its sniffed format; None
    when it is not an image format we keep."""
    extension = image_extension(data)
    if extension is None:
        return None
    return hashlib.sha256(data).hexdigest()[:ASSET_NAME_HEX] + extension


def link_to(asset: PurePosixPath, page_folder: PurePosixPath) -> str:
    """The relative link from a page in ``page_folder`` to a KB-relative ``asset``."""
    return "../" * len(page_folder.parts) + asset.as_posix()
