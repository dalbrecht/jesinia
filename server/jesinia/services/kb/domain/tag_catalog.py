# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``Tags`` catalog page: one ``- #tag: meaning (aliases: a, b)`` line per approved tag."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from jesinia.services.kb.domain.tags import extract_tags, has_tag_grammar, normalise_tag

CATALOG_TITLE = "Tags"
CATALOG_KIND = "tag-catalog"

_ENTRY_LINE = re.compile(r"^- #(?P<tag>[^\s:(]+)(?P<rest>.*)$")
_TRAILING_GROUP = re.compile(r"\s*\((?P<body>aliases:[^()]*|retired|ignored)\)\s*$")


def indexed_tags(body: str, jesinia: Mapping[str, object]) -> list[str]:
    """The tags a page uses, for the index. The catalog page lists tags; it never uses them,
    so an approved tag no page uses has no uses (and no page to rename)."""
    return [] if jesinia.get("kind") == CATALOG_KIND else extract_tags(body)


@dataclass(frozen=True)
class CatalogEntry:
    """One approved (or retired, or ignored) tag."""

    tag: str
    meaning: str = ""
    aliases: tuple[str, ...] = ()
    retired: bool = False
    ignored: bool = False


@dataclass(frozen=True)
class TagCatalog:
    """Entries by canonical tag, plus the page lines (unknown lines are kept verbatim)."""

    entries: Mapping[str, CatalogEntry]
    lines: tuple[str, ...]

    def resolve(self, tag: str) -> str | None:
        """The canonical tag for a tag or alias, or None (ignored tags never resolve)."""
        wanted = normalise_tag(tag)
        approved = [entry for entry in self.entries.values() if not entry.ignored]
        for entry in approved:
            if entry.tag == wanted:
                return entry.tag
        for entry in approved:
            if wanted in entry.aliases:
                return entry.tag
        return None


def _parse_entry(line: str) -> CatalogEntry | None:
    match = _ENTRY_LINE.match(line)
    if match is None or not has_tag_grammar(match.group("tag")):
        return None
    rest = match.group("rest").rstrip()
    retired = ignored = False
    aliases: tuple[str, ...] = ()
    while (group := _TRAILING_GROUP.search(rest)) is not None:
        body = group.group("body")
        if body == "retired":
            retired = True
        elif body == "ignored":
            ignored = True
        else:
            names = (normalise_tag(name) for name in body.removeprefix("aliases:").split(","))
            aliases = tuple(name for name in names if name)
        rest = rest[: group.start()]
    if rest.startswith(":"):
        meaning = rest[1:].strip()
    elif rest.strip() == "":
        meaning = ""
    else:
        return None
    return CatalogEntry(normalise_tag(match.group("tag")), meaning, aliases, retired, ignored)


def _merge(first: CatalogEntry, later: CatalogEntry) -> CatalogEntry:
    aliases = tuple(dict.fromkeys((*first.aliases, *later.aliases)))
    return replace(first, meaning=first.meaning or later.meaning, aliases=aliases)


def parse_catalog(body: str) -> TagCatalog:
    """Lenient parse; duplicates merge (first non-empty meaning wins, aliases are united)."""
    lines = tuple(body.split("\n"))
    entries: dict[str, CatalogEntry] = {}
    for line in lines:
        entry = _parse_entry(line)
        if entry is not None:
            existing = entries.get(entry.tag)
            entries[entry.tag] = entry if existing is None else _merge(existing, entry)
    return TagCatalog(MappingProxyType(entries), lines)


def _render_entry(entry: CatalogEntry) -> str:
    text = f"- #{entry.tag}"
    if entry.meaning:
        text += f": {entry.meaning}"
    if entry.aliases:
        text += f" (aliases: {', '.join(entry.aliases)})"
    if entry.retired:
        text += " (retired)"
    if entry.ignored:
        text += " (ignored)"
    return text


def render_catalog(cat: TagCatalog) -> str:
    """Entries stay in their original slots, once each; new entries are appended."""
    written: set[str] = set()
    out: list[str] = []
    for line in cat.lines:
        entry = _parse_entry(line)
        if entry is None:
            out.append(line)
        elif entry.tag in cat.entries and entry.tag not in written:
            written.add(entry.tag)
            out.append(_render_entry(cat.entries[entry.tag]))
    appended = [_render_entry(e) for tag, e in cat.entries.items() if tag not in written]
    if appended:
        if out and out[-1] == "":
            out[-1:] = [*appended, ""]
        else:
            out.extend(appended)
    return "\n".join(out)


def with_entry(cat: TagCatalog, entry: CatalogEntry) -> TagCatalog:
    """A catalog with ``entry`` added or replacing the entry of the same tag.

    The tag and aliases are normalised first, because callers pass user input.
    """
    aliases = (normalise_tag(alias) for alias in entry.aliases)
    entry = replace(entry, tag=normalise_tag(entry.tag), aliases=tuple(a for a in aliases if a))
    return TagCatalog(MappingProxyType({**cat.entries, entry.tag: entry}), cat.lines)


def without_entry(cat: TagCatalog, tag: str) -> TagCatalog:
    """A catalog without ``tag``'s entry; its line is dropped when the page is rendered."""
    wanted = normalise_tag(tag)
    entries = {key: entry for key, entry in cat.entries.items() if key != wanted}
    return TagCatalog(MappingProxyType(entries), cat.lines)
