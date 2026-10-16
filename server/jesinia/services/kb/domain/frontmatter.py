# SPDX-License-Identifier: AGPL-3.0-or-later
"""Front-matter codec (spec §5.2, §5.4).

Two write paths:

- ``serialize_page`` re-emits the whole document in canonical order (``id``, ``title``,
  foreign keys in their original order, ``jesinia`` last). Used for new pages and explicit
  front-matter edits.
- ``replace_body`` keeps the raw front-matter block byte-for-byte and swaps only the body.
  Used for ordinary edits, so user YAML formatting is never rewritten (ADR 0008 §5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

import yaml

from jesinia.services.kb.domain.page import Page, PageId

Kind = Literal["page", "plain", "foreign"]
_OPEN = ("---\n", "---\r\n")
_CLOSE = frozenset({"---", "..."})


@dataclass(frozen=True)
class ParsedDoc:
    kind: Kind
    frontmatter: dict[str, object] | None
    body: str


def split_document(text: str) -> tuple[str | None, str]:
    """Return ``(raw front-matter block, body)``; the block includes both fence lines."""
    if not text.startswith(_OPEN):
        return None, text
    pos = text.index("\n") + 1
    while pos < len(text):
        end = text.find("\n", pos)
        line_end = len(text) if end == -1 else end + 1
        if text[pos:line_end].rstrip("\r\n") in _CLOSE:
            return text[:line_end], text[line_end:]
        pos = line_end
    return None, text


def _yaml_inner(raw: str) -> str:
    lines = raw.splitlines(keepends=True)
    return "".join(lines[1:-1])


def parse_document(text: str) -> ParsedDoc:
    if not text.startswith(_OPEN):
        return ParsedDoc("plain", None, text)
    raw, body = split_document(text)
    if raw is None:
        return ParsedDoc("foreign", None, text)  # unclosed fence: never rewrite
    try:
        data = yaml.safe_load(_yaml_inner(raw))
    except yaml.YAMLError:
        return ParsedDoc("foreign", None, body)
    if not isinstance(data, dict):
        return ParsedDoc("foreign", None, body)
    fm = {str(k): v for k, v in data.items()}
    return ParsedDoc("page" if "id" in fm else "foreign", fm, body)


def page_from(doc: ParsedDoc, path: PurePosixPath) -> Page:
    if doc.kind != "page" or doc.frontmatter is None:
        raise ValueError(f"{path}: not a Jesinia page ({doc.kind})")
    fm = doc.frontmatter
    jesinia = fm.get("jesinia", {}) or {}
    if not isinstance(jesinia, Mapping):
        raise ValueError(f"{path}: 'jesinia' front-matter must be a mapping")
    title = fm.get("title")
    foreign = {k: v for k, v in fm.items() if k not in ("id", "title", "jesinia")}
    return Page(
        id=PageId(str(fm["id"])),
        title=str(title) if title is not None else path.stem,
        path=path,
        body=doc.body,
        jesinia=dict(jesinia),
        foreign=foreign,
    )


def title_scalar(title: str) -> str:
    """Bare when it round-trips as a YAML scalar, else JSON-quoted (spec §5.2)."""
    if title and "\n" not in title:
        try:
            if yaml.safe_load(f"t: {title}") == {"t": title}:
                return title
        except yaml.YAMLError:
            pass
    return json.dumps(title, ensure_ascii=False)


def _dump(mapping: Mapping[str, object]) -> str:
    return yaml.safe_dump(
        dict(mapping), sort_keys=False, allow_unicode=True, default_flow_style=False
    )


def serialize_page(page: Page) -> str:
    out = ["---\n", f"id: {page.id}\n", f"title: {title_scalar(page.title)}\n"]
    if page.foreign:
        out.append(_dump(page.foreign))
    if page.jesinia:
        out.append(_dump({"jesinia": dict(page.jesinia)}))
    out.append("---\n")
    out.append(page.body)
    return "".join(out)


def replace_body(text: str, body: str) -> str:
    """Swap the body, keeping the raw front-matter block exactly as it is on disk."""
    raw, _ = split_document(text)
    return (raw or "") + body
