# SPDX-License-Identifier: AGPL-3.0-or-later
"""``TagService``: the KB-wide tag catalog, checks, suggestions, rename and merge (spec §5).

The catalog is a KB page (``Tags.md`` at the root, ``jesinia.kind: tag-catalog``); every
change to it, and every page rewrite, goes through ``KbService`` with a base revision.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Protocol

from jesinia.services.kb.application.errors import (
    InvalidTag,
    KbError,
    NearDuplicate,
    PageNotFound,
    StaleWrite,
    TagExists,
    TagNotFound,
)
from jesinia.services.kb.application.service import KbService, PageDraft, PageWithRev, nfc
from jesinia.services.kb.domain.frontmatter import parse_document
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.paths import is_page_candidate, is_reserved
from jesinia.services.kb.domain.tag_catalog import (
    CATALOG_KIND,
    CATALOG_TITLE,
    CatalogEntry,
    TagCatalog,
    parse_catalog,
    render_catalog,
    with_entry,
    without_entry,
)
from jesinia.services.kb.domain.tags import (
    extract_tags,
    is_valid_tag,
    near_matches,
    normalise_tag,
    replace_tag,
)

log = logging.getLogger(__name__)

KB_ROOT = PurePosixPath("")
CATALOG_PATH = PurePosixPath(f"{CATALOG_TITLE}.md")
_WORD = re.compile(r"[^\W_]+")


class TagAdvisor(Protocol):
    """An agent that proposes tags and spots synonyms (implemented in infrastructure)."""

    async def suggest(self, text: str, catalog: list[CatalogEntry]) -> list[str]:
        """Up to five tags for ``text``, preferring ``catalog`` tags when one fits."""
        ...

    async def same_meaning(self, tag: str, catalog: list[CatalogEntry]) -> str | None:
        """The catalog tag that means the same as ``tag``, if any."""
        ...


@dataclass(frozen=True)
class TagInfo:
    tag: str
    meaning: str
    aliases: tuple[str, ...]
    retired: bool
    count: int  # pages using the tag or one of its aliases (the catalog page excluded)


@dataclass(frozen=True)
class TagCheck:
    normalised: str
    exists: bool
    canonical: str | None
    near: list[str]
    same_meaning: str | None


@dataclass(frozen=True)
class Suggestion:
    tag: str
    new: bool  # not in the catalog: accepting it means creating it


@dataclass(frozen=True)
class PageRef:
    id: PageId
    title: str
    rev: str


@dataclass(frozen=True)
class RenamePreview:
    pages: list[PageRef]
    skipped_reserved: list[str]  # reserved-folder files using the tag; never written


@dataclass(frozen=True)
class RenameResult:
    updated: list[PageId]
    skipped_changed: list[PageId]  # changed since the preview (or gone); left alone


@dataclass(frozen=True)
class _Loaded:
    catalog: TagCatalog
    page: PageWithRev | None  # None until the first catalog write creates the page

    @property
    def page_id(self) -> PageId | None:
        return self.page.page.id if self.page is not None else None


def _tag(raw: str) -> str:
    tag = normalise_tag(raw)
    if not is_valid_tag(tag):
        raise InvalidTag(f"{raw!r} is not a valid tag (a letter or digit, then letters, digits, -)")
    return tag


def _meaning(raw: str) -> str:
    """One line of text that reads back as itself from the catalog page."""
    meaning = " ".join(raw.split())
    probe = CatalogEntry("probe", meaning)
    if parse_catalog(render_catalog(with_entry(parse_catalog(""), probe))).entries != {
        "probe": probe
    }:
        raise InvalidTag(f"{meaning!r} cannot be stored as a tag meaning")
    return meaning


def _approved(catalog: TagCatalog) -> list[CatalogEntry]:
    return [entry for entry in catalog.entries.values() if not entry.ignored]


def _phrase(text: str) -> str:
    return " ".join(_WORD.findall(unicodedata.normalize("NFC", text).lower()))


def _first_mention(entry: CatalogEntry, words: str) -> int | None:
    """Where ``entry`` (or an alias, singular or plural) first appears in ``words``."""
    hits = [
        words.find(f" {form} ")
        for name in (entry.tag, *entry.aliases)
        for form in (_phrase(name), _phrase(name) + "s")
    ]
    found = [hit for hit in hits if hit >= 0]
    return min(found) if found else None


def _merged(catalog: TagCatalog, sources: list[str], target: str) -> TagCatalog:
    """``target`` absorbs each source: they become its aliases, keeping theirs."""
    existing = catalog.entries.get(target)
    base = existing if existing is not None and not existing.ignored else CatalogEntry(target)
    meaning, aliases = base.meaning, list(base.aliases)
    for source in sources:
        entry = catalog.entries.get(source)
        if entry is not None and not entry.ignored:
            meaning = meaning or entry.meaning
            aliases.extend((source, *entry.aliases))
        else:
            aliases.append(source)
            owner = catalog.resolve(source)
            if owner is not None and owner != target:
                # An alias moves: the entry that held it must let go, or it keeps resolving there.
                held = catalog.entries[owner]
                kept_aliases = tuple(alias for alias in held.aliases if alias != source)
                catalog = with_entry(catalog, replace(held, aliases=kept_aliases))
        catalog = without_entry(catalog, source)
    kept = tuple(dict.fromkeys(alias for alias in aliases if alias != target))
    return with_entry(catalog, replace(base, meaning=meaning, aliases=kept))


class TagService:
    """Catalog reads, checks and suggestions, and the user's catalog and rename writes."""

    def __init__(self, kb: KbService, advisor: TagAdvisor | None) -> None:
        self._kb = kb
        self._advisor = advisor
        # Catalog edits are read-modify-write on one page; serialise them in-process so two
        # quick adds don't turn into a StaleWrite (the base revision still guards editors).
        self._catalog_lock = asyncio.Lock()

    # -- catalog page --------------------------------------------------------------
    async def _load(self) -> _Loaded:
        """The catalog page: a root ``Tags`` page of kind tag-catalog, ``Tags.md`` first."""
        rows = [
            row
            for row in self._kb.index.all()
            if row.path.parent == KB_ROOT and row.title == CATALOG_TITLE
        ]
        rows.sort(key=lambda row: (row.path != CATALOG_PATH, str(row.path)))
        for row in rows:
            try:
                current = await self._kb.read(row.id)
            except (PageNotFound, OSError, ValueError) as exc:
                log.warning("tag catalog candidate %s is unreadable: %s", row.path, exc)
                continue
            if current.page.jesinia.get("kind") == CATALOG_KIND:
                return _Loaded(parse_catalog(current.page.body), current)
        return _Loaded(parse_catalog(""), None)

    async def _save(self, loaded: _Loaded, catalog: TagCatalog) -> PageId:
        """Write the catalog page (creating it at the KB root on first use); its id."""
        body = render_catalog(catalog)
        if loaded.page is None:
            draft = PageDraft(None, CATALOG_TITLE, KB_ROOT, body, {"kind": CATALOG_KIND})
            return (await self._kb.write(draft)).page.id
        saved = await self._kb.update_body(loaded.page.page.id, body, expected_rev=loaded.page.rev)
        return saved.page.id

    def _pages_with(self, names: Iterable[str], catalog_id: PageId | None) -> list[PageId]:
        """Pages using any of ``names``, in index order, without the catalog page itself."""
        found = (page for name in names for page in self._kb.index.pages_with_tag(name))
        return [page for page in dict.fromkeys(found) if page != catalog_id]

    async def _advise[T](self, ask: Callable[[TagAdvisor], Awaitable[T]], fallback: T) -> T:
        if self._advisor is None:
            return fallback
        try:
            return await ask(self._advisor)
        # The advisor is a best-effort agent runner (a derived subsystem): whatever it raises,
        # checks and suggestions degrade to catalog-only answers rather than failing.
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning(
                "tag advisor failed (%s: %s); using the catalog alone", type(exc).__name__, exc
            )
            return fallback

    @staticmethod
    def _near(catalog: TagCatalog, tag: str, canonical: str | None) -> list[str]:
        names = [name for entry in _approved(catalog) for name in (entry.tag, *entry.aliases)]
        near: dict[str, None] = {}
        for name in near_matches(tag, names):
            resolved = catalog.resolve(name)
            if resolved is not None and resolved != canonical:
                near.setdefault(resolved, None)
        return list(near)

    # -- reads ---------------------------------------------------------------------
    async def catalog(self) -> list[TagInfo]:
        """Approved and retired tags, in catalog order, with their page counts."""
        loaded = await self._load()
        return [
            TagInfo(
                entry.tag,
                entry.meaning,
                entry.aliases,
                entry.retired,
                len(self._pages_with((entry.tag, *entry.aliases), loaded.page_id)),
            )
            for entry in _approved(loaded.catalog)
        ]

    async def check(self, raw: str, *, advise: bool = True) -> TagCheck:
        """Normalise ``raw`` and say whether it, or something close, is already a tag.

        The advisor (an agent run) is asked for a same-meaning tag only when ``advise`` is
        set, the tag is not in the catalog and the catalog has approved tags to compare with.
        """
        tag = _tag(raw)
        catalog = (await self._load()).catalog
        canonical = catalog.resolve(tag)
        same: str | None = None
        if advise and canonical is None and _approved(catalog):
            answer = await self._advise(lambda a: a.same_meaning(tag, _approved(catalog)), None)
            same = catalog.resolve(answer) if answer else None
        return TagCheck(
            tag, canonical is not None, canonical, self._near(catalog, tag, canonical), same
        )

    async def suggest(self, text: str, *, limit: int = 5, advise: bool = True) -> list[Suggestion]:
        """Catalog tags mentioned in ``text`` first, then (when ``advise``) the advisor's;
        never retired or ignored ones. A tag outside the catalog is flagged ``new``."""
        catalog = (await self._load()).catalog
        live = [entry for entry in _approved(catalog) if not entry.retired]
        words = f" {_phrase(text)} "
        mentioned = [(at, order, e.tag) for order, e in enumerate(live)
                     if (at := _first_mention(e, words)) is not None]  # fmt: skip
        picked: dict[str, Suggestion] = {
            tag: Suggestion(tag, False) for _, _, tag in sorted(mentioned)
        }
        if advise and len(picked) < limit:
            for raw in await self._advise(lambda a: a.suggest(text, live), list[str]()):
                tag = normalise_tag(raw)
                entry = catalog.entries.get(tag)
                if not is_valid_tag(tag) or (entry is not None and entry.ignored):
                    continue
                canonical = catalog.resolve(tag)
                if canonical is None:
                    picked.setdefault(tag, Suggestion(tag, True))
                elif not catalog.entries[canonical].retired:
                    picked.setdefault(canonical, Suggestion(canonical, False))
        return list(picked.values())[:limit]

    async def uncataloged(self) -> list[tuple[str, int]]:
        """Hashtags used in pages but neither in the catalog nor ignored, most used first."""
        loaded = await self._load()
        catalog = loaded.catalog
        found: list[tuple[str, int]] = []
        for tag in self._kb.index.tag_counts():
            if tag in catalog.entries or catalog.resolve(tag) is not None:
                continue
            count = len(self._pages_with((tag,), loaded.page_id))
            if count:
                found.append((tag, count))
        return sorted(found, key=lambda item: (-item[1], item[0]))

    # -- catalog writes --------------------------------------------------------------
    async def add(self, tag: str, meaning: str = "", *, force: bool = False) -> None:
        """Approve a new tag. ``force`` (an explicit user override) skips the near-match
        check; an exact or alias match always raises ``TagExists``."""
        wanted = _tag(tag)
        text = _meaning(meaning)
        async with self._catalog_lock:
            loaded = await self._load()
            catalog = loaded.catalog
            canonical = catalog.resolve(wanted)
            if canonical is not None:
                raise TagExists(f"#{wanted} is already in the catalog as #{canonical}")
            if not force and (near := self._near(catalog, wanted, None)):
                raise NearDuplicate(wanted, near)
            await self._save(loaded, with_entry(catalog, CatalogEntry(wanted, text)))

    async def ensure(self, tags: Iterable[str]) -> list[str]:
        """The canonical catalog names for ``tags`` (aliases resolved, duplicates dropped),
        adding the ones not in the catalog in a single write.

        All or nothing: every tag is validated and near-checked (against the catalog and the
        other new tags) before anything is written, so ``InvalidTag`` or ``NearDuplicate``
        leaves the catalog untouched.
        """
        wanted = [_tag(raw) for raw in tags]
        async with self._catalog_lock:
            loaded = await self._load()
            catalog = loaded.catalog
            names: dict[str, None] = {}
            for tag in wanted:
                canonical = catalog.resolve(tag)
                if canonical is None:
                    if near := self._near(catalog, tag, None):
                        raise NearDuplicate(tag, near)
                    catalog = with_entry(catalog, CatalogEntry(tag))
                    canonical = tag
                names.setdefault(canonical, None)
            if catalog is not loaded.catalog:
                await self._save(loaded, catalog)
        return list(names)

    async def adopt(self, tag: str) -> None:
        """Approve a tag already used in pages (from the uncataloged list)."""
        await self.add(tag, force=True)

    async def retire(self, tag: str) -> None:
        """Keep the tag in pages but stop suggesting it."""
        await self._set_retired(tag, retired=True)

    async def restore(self, tag: str) -> None:
        """Undo ``retire``: suggest the tag again."""
        await self._set_retired(tag, retired=False)

    async def _set_retired(self, tag: str, *, retired: bool) -> None:
        wanted = _tag(tag)
        async with self._catalog_lock:
            loaded = await self._load()
            canonical = loaded.catalog.resolve(wanted)
            if canonical is None:
                raise TagNotFound(f"#{wanted} is not in the catalog")
            entry = replace(loaded.catalog.entries[canonical], retired=retired)
            await self._save(loaded, with_entry(loaded.catalog, entry))

    async def ignore(self, tag: str) -> None:
        """Record ``- #tag (ignored)`` so the tag leaves the uncataloged list."""
        wanted = _tag(tag)
        async with self._catalog_lock:
            loaded = await self._load()
            canonical = loaded.catalog.resolve(wanted)
            if canonical is not None:
                raise TagExists(f"#{wanted} is in the catalog as #{canonical}; retire it instead")
            entry = CatalogEntry(wanted, ignored=True)
            await self._save(loaded, with_entry(loaded.catalog, entry))

    # -- rename and merge --------------------------------------------------------------
    def _reserved_using(self, tags: set[str]) -> list[str]:
        """Reserved-folder files that use any of ``tags``: read only, to list them."""
        files = self._kb.files
        if files is None:
            return []
        found: list[str] = []
        for disk in files.walk():
            rel = nfc(disk)
            if not (is_reserved(rel) and is_page_candidate(rel)):
                continue
            try:
                body = parse_document(files.read(disk).decode("utf-8")).body
            except (OSError, ValueError) as exc:
                log.warning("tag rename preview skipped %s: %s", rel, exc)
                continue
            if tags & set(extract_tags(body)):
                found.append(str(rel))
        return sorted(found)

    async def preview_rename(self, old: str, new: str) -> RenamePreview:
        """The pages a rename would rewrite (with base revisions) and the reserved-folder
        files it will leave alone."""
        old_tag, new_tag = _tag(old), _tag(new)
        if old_tag == new_tag:
            raise InvalidTag(f"#{old_tag} and #{new_tag} are the same tag")
        loaded = await self._load()
        pages: list[PageRef] = []
        for page in self._pages_with((old_tag,), loaded.page_id):
            row = self._kb.index.get(page)
            if row is not None and not is_reserved(row.path):
                pages.append(PageRef(page, row.title, row.rev))
        pages.sort(key=lambda ref: (ref.title.casefold(), ref.id))
        return RenamePreview(pages, self._reserved_using({old_tag}))

    async def apply_rename(self, old: str, new: str, revs: Mapping[PageId, str]) -> RenameResult:
        """Rewrite ``#old`` as ``#new`` in pages still at their previewed revision, and
        record ``old`` as an alias of ``new``."""
        return await self.merge([old], new, revs)

    async def merge(
        self, sources: list[str], into: str, revs: Mapping[PageId, str]
    ) -> RenameResult:
        """Rename every source into ``into`` (one write per page); aliases are kept.

        The catalog is updated first, so a page skipped below still resolves through the
        new alias. Reserved-folder pages are never written, even when ``revs`` names them.
        """
        target_raw = _tag(into)
        wanted = list(dict.fromkeys(_tag(source) for source in sources))
        async with self._catalog_lock:
            loaded = await self._load()
            target = loaded.catalog.resolve(target_raw) or target_raw
            old_tags = [tag for tag in wanted if tag != target]
            if not old_tags:
                raise InvalidTag(f"nothing to rename into #{target}")
            catalog_id = await self._save(loaded, _merged(loaded.catalog, old_tags, target))
        updated: list[PageId] = []
        skipped: list[PageId] = []
        for page in self._pages_with(old_tags, catalog_id):
            row = self._kb.index.get(page)
            if row is None or is_reserved(row.path):
                continue
            base = revs.get(page)
            if base is None:
                skipped.append(page)  # tagged after the preview: not part of what was approved
                continue
            try:
                current = await self._kb.read(page)
                if current.rev != base:
                    raise StaleWrite(f"{page}: changed since the preview")
                body = current.page.body
                for tag in old_tags:
                    body = replace_tag(body, tag, target)
                if body != current.page.body:
                    await self._kb.update_body(page, body, expected_rev=base)
                    updated.append(page)
            except (KbError, OSError, ValueError) as exc:
                log.info("tag rename skipped %s: %s", page, exc)
                skipped.append(page)
        return RenameResult(sorted(updated), sorted(skipped))
