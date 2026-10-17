# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layered review standards (spec §5.2). Pure: no I/O.

Three layers, applied in order, later ones winning:

- ``base``: the author's coding-standards checkout, filtered to the repo's languages (per
  the repo's ``.standards.yml``) plus the common documents;
- ``repo``: ``.jesinia/review.md`` in the reviewed repo, at the tip of its default branch
  (never PR content);
- ``user``: the author's own documents, then the overrides stored in ``review.sqlite``.

A rule is a Markdown section, identified by its document and heading path
(``lang-01_python_standards.md › Security › P1``). A later layer replaces a rule by naming
it: a heading written as a rule ID (``# lang-01_python_standards.md › Security``) targets
that section, and replaces it together with everything beneath it. Any other heading is a
new rule of the document it is in. A replacement with no text removes the rule.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from jesinia.apps.review.domain.errors import ReviewError

Layer = Literal["base", "repo", "user"]
LAYERS: tuple[Layer, ...] = ("base", "repo", "user")
RULE_SEPARATOR = " › "
# Every standards document (base files, the repo's file, user files) is refused past this.
MAX_DOCUMENT_BYTES = 256 * 1024
# The repo's .standards.yml is a short config: refused past this, and past this nesting.
MAX_CONFIG_BYTES = 64 * 1024
MAX_CONFIG_NESTING = 32
# Rules per document, and headings in a rule ID: past these a document is refused (or a
# heading isn't read as a rule ID), so layering stays fast whatever a repo commits.
MAX_SECTIONS = 2_000
MAX_RULE_DEPTH = 12
REPO_STANDARDS_CONFIG = ".standards.yml"
REPO_REVIEW_FILE = ".jesinia/review.md"

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LANGUAGE_DOC = re.compile(r"^lang-\d+_(.+)_standards\.md$")
# Names in .standards.yml that stand for several language documents.
_LANGUAGE_ALIASES: Mapping[str, tuple[str, ...]] = {
    "jvm": ("java", "kotlin"),
    "rails": ("ruby_on_rails", "ruby"),
}


class StandardsError(ReviewError):
    """Base for standards that can't be loaded or applied."""


class InvalidStandardsConfigError(StandardsError):
    """The repo's ``.standards.yml`` isn't valid YAML of the documented shape."""


class TooManyStandardsSectionsError(StandardsError):
    """A standards document has more than ``MAX_SECTIONS`` rules."""


class StandardsDocumentTooLargeError(StandardsError):
    """A standards document is over ``MAX_DOCUMENT_BYTES``; it was not read."""


class StandardsUnreadableError(StandardsError):
    """A standards layer couldn't be read (the mirror lacks the commit, git failed)."""


class InvalidOverrideError(StandardsError):
    """A stored override that names no rule."""


@dataclass(frozen=True)
class Document:
    """One standards document: its name (what rule IDs start with), layer and Markdown."""

    name: str
    layer: Layer
    text: str


@dataclass(frozen=True)
class Section:
    """One rule: the text under one heading, up to the next heading of any level."""

    doc: str
    heading_path: tuple[str, ...]
    layer: Layer
    text: str


@dataclass(frozen=True)
class Override:
    """A rule set in the app (Settings → Review), applied on top of every layer."""

    doc: str
    heading_path: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        if not self.doc.strip():
            raise InvalidOverrideError("an override must name a document")
        if any(not heading.strip() for heading in self.heading_path):
            raise InvalidOverrideError(f"an override has an empty heading: {self.heading_path!r}")
        if len(self.heading_path) > MAX_RULE_DEPTH:
            raise InvalidOverrideError(f"an override is nested too deep (over {MAX_RULE_DEPTH})")


@dataclass(frozen=True)
class Standards:
    """The merged rules, in reading order."""

    sections: list[Section]

    @staticmethod
    def rule_id(section: Section) -> str:
        """``doc › h1 › h2``: how findings and metrics name the rule they apply."""
        return RULE_SEPARATOR.join((section.doc, *section.heading_path))

    def as_prompt(self, limit: int | None = None) -> str:
        """The rules as Markdown for an agent, each headed by its rule ID and layer.

        With ``limit`` (characters), rules are left out until the text fits: the base
        layer's from its end first, then the repo layer's, then the user layer's, and the
        text says how many were left out from which layer.
        """
        if not self.sections:
            return (
                "# Review standards\n\nNo review standards are configured: judge by general "
                "good practice, and leave each finding's rule empty."
            )
        blocks = [
            f"## {self.rule_id(section)} [{section.layer}]\n\n{section.text}"
            for section in self.sections
        ]
        kept = [True] * len(blocks)
        dropped: Counter[str] = Counter()

        def render() -> str:
            notes = "".join(
                f"Note: rules from the {layer} layer were left out ({dropped[layer]} of them) "
                f"because the standards exceed this prompt's {limit}-character budget.\n\n"
                for layer in LAYERS
                if dropped[layer]
            )
            rules = "\n\n".join(block for block, keep in zip(blocks, kept, strict=True) if keep)
            return f"{_PROMPT_HEADER}{notes}{rules}"

        size = len(render())
        for layer in LAYERS:
            for index in reversed(range(len(blocks))):
                if limit is None or size <= limit:
                    break
                if self.sections[index].layer == layer:
                    kept[index] = False
                    dropped[layer] += 1
                    size = len(render()) if dropped[layer] == 1 else size - len(blocks[index]) - 2
        text = render()
        return text if limit is None else text[:limit]


_PROMPT_HEADER = (
    "# Review standards\n\n"
    "Layers, earliest first: base (the author's coding standards), repo (the repo's "
    ".jesinia/review.md on its default branch) and user (the author's own overrides); later "
    "layers have already replaced what they override. When a finding or metric applies a "
    "rule, cite its rule ID (the heading below, without the layer) and its layer.\n\n"
)


def _target(heading: str) -> tuple[str, tuple[str, ...]] | None:
    """A heading written as a rule ID, as ``(doc, heading path)``; else ``None``."""
    if RULE_SEPARATOR.strip() not in heading:
        return None
    parts = [part.strip() for part in heading.split(RULE_SEPARATOR.strip())]
    if any(not part for part in parts) or len(parts) - 1 > MAX_RULE_DEPTH:
        return None
    return parts[0], tuple(parts[1:])


def split_sections(document: Document) -> list[Section]:
    """``document``'s rules: text before the first heading (a rule named by the document
    alone, if not blank) and the text under each ATX heading. Headings inside fenced code
    blocks are text."""
    sections: list[Section] = []
    # (level, doc, path) of the open headings, innermost last.
    stack: list[tuple[int, str, tuple[str, ...]]] = []
    doc: str = document.name
    path: tuple[str, ...] = ()
    lines: list[str] = []
    fence: str | None = None

    def close() -> None:
        text = "\n".join(lines).strip()
        if path or text:
            if len(sections) == MAX_SECTIONS:
                raise TooManyStandardsSectionsError(
                    f"{document.name} has more than {MAX_SECTIONS} rules"
                )
            sections.append(Section(doc, path, document.layer, text))

    for line in document.text.splitlines():
        if fence is not None:
            if line.lstrip().startswith(fence):
                fence = None
            lines.append(line)
            continue
        if opened := _FENCE.match(line):
            fence = opened.group(1)
            lines.append(line)
            continue
        heading = _HEADING.match(line)
        if heading is None:
            lines.append(line)
            continue
        close()
        level, title = len(heading.group(1)), heading.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        target = _target(title)
        if target is not None:
            doc, path = target
        else:
            parent_doc, parent_path = (stack[-1][1], stack[-1][2]) if stack else (document.name, ())
            doc, path = parent_doc, (*parent_path, title)
        stack.append((level, doc, path))
        lines = []
    close()
    return sections


Key = tuple[str, tuple[str, ...]]


def layer_standards(documents: Iterable[Document], overrides: Sequence[Override]) -> Standards:
    """Merge ``documents`` layer by layer (base, repo, user; the order given within a
    layer), then the stored ``overrides``. A rule from a later source replaces the earlier
    rule with its ID, in its place, together with every rule beneath it from earlier
    sources; a replacement with blank text removes the rule. Rules with no text of their
    own are left out.

    Linear in the number of rules: each rule is indexed under every heading above it, so
    a replacement finds the rules beneath it without scanning the rest.
    """
    ordered = sorted(documents, key=lambda document: LAYERS.index(document.layer))
    # (source, section): each document is one source and each override another, in order.
    incoming = [
        (source, section)
        for source, document in enumerate(ordered)
        for section in split_sections(document)
    ]
    incoming += [
        (len(ordered) + index, Section(o.doc, o.heading_path, "user", o.text.strip()))
        for index, o in enumerate(overrides)
    ]
    merged: dict[Key, tuple[int, Section]] = {}
    beneath: dict[Key, set[Key]] = {}

    def prefixes(key: Key) -> Iterable[Key]:
        return ((key[0], key[1][:depth]) for depth in range(len(key[1])))

    for source, section in incoming:
        key = (section.doc, section.heading_path)
        for other in [o for o in beneath.get(key, ()) if merged[o][0] < source]:
            del merged[other]
            for prefix in prefixes(other):
                beneath[prefix].discard(other)
        if key not in merged:
            for prefix in prefixes(key):
                beneath.setdefault(prefix, set()).add(key)
        merged[key] = (source, section)  # an existing key keeps its place in the order
    return Standards([section for _, section in merged.values() if section.text.strip()])


def check_config_nesting(text: str) -> None:
    """Refuse a ``.standards.yml`` whose flow collections (``[``/``{``) nest deeper than
    ``MAX_CONFIG_NESTING``, before a YAML parser recurses into it. Quoted strings and
    comments don't count.

    Raises:
        InvalidStandardsConfigError: nested too deep.
    """
    depth = 0
    quote: str | None = None
    escaped = comment = False
    previous = "\n"
    for char in text:
        if comment:
            comment = char != "\n"
        elif quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and previous in " \t\n":
            comment = True
        elif char in "[{":
            depth += 1
            if depth > MAX_CONFIG_NESTING:
                raise InvalidStandardsConfigError(
                    f"{REPO_STANDARDS_CONFIG} nests deeper than {MAX_CONFIG_NESTING} levels"
                )
        elif char in "]}":
            depth = max(0, depth - 1)
        previous = char


def parse_languages(config: object) -> tuple[str, ...]:
    """The ``languages`` of a parsed ``.standards.yml`` (empty when it lists none).

    Raises:
        InvalidStandardsConfigError: not a mapping, or ``languages`` isn't a list of names.
    """
    if config is None:
        return ()
    if not isinstance(config, Mapping):
        raise InvalidStandardsConfigError(".standards.yml must be a mapping")
    languages = config.get("languages", [])
    if not isinstance(languages, list) or not all(isinstance(name, str) for name in languages):
        raise InvalidStandardsConfigError(".standards.yml languages must be a list of names")
    return tuple(languages)


def expand_languages(languages: Iterable[str]) -> frozenset[str]:
    """The language document names (``python``, ``ruby_on_rails``) the config's languages
    stand for: ``jvm`` is Java and Kotlin, and ``rails`` brings Ruby with it."""
    names: set[str] = set()
    for language in languages:
        key = language.strip().lower()
        names.update(_LANGUAGE_ALIASES.get(key, (key,)))
    return frozenset(names)


def applies(doc_name: str, languages: frozenset[str]) -> bool:
    """Whether a base document applies: language documents (``lang-NN_<language>_
    standards.md``) only for the repo's languages, every other document always."""
    language = _LANGUAGE_DOC.match(doc_name)
    return language is None or language.group(1) in languages
