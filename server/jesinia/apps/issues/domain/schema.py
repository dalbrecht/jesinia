# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-repo label schema (spec §2, §6.2).

A schema lists, per dimension, the canonical label values and the existing GitHub labels that
mean the same thing (aliases). It comes from the repo's ``.jesinia/labels.yml`` when present,
otherwise from Jesinia's settings, starting from ``DEFAULT_SCHEMA``.

``.jesinia/labels.yml`` is untrusted repo content, so ``parse_labels_yml`` is lenient: it never
raises, it caps the document's size and flow nesting before YAML sees it, and every unusable
key or entry becomes a line in ``LabelSchema.errors`` while the rest of the schema loads. The
format::

    readiness:            # likewise priority and type; a missing one keeps the default
      - agent-ready
      - name: design-needed
        aliases: [design needed]
    areas:                # names start with "area:"; aliases may be any existing label
      - name: area:apple
        aliases: [platform:ios, apple]
    epic: epic            # or {name: epic, aliases: [...]}; initiative likewise
    free_prefixes: ["ops:"]   # added to review:, campaign: and theme:

Labels match case-insensitively, as GitHub's label names do.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache

import yaml

MAX_LABELS_YML_BYTES = 64 * 1024
# Flow collections ("[", "{") deeper than this are refused before the YAML parser recurses.
MAX_LABELS_YML_NESTING = 32
MAX_ENTRIES = 200
MAX_ALIASES = 50
# GitHub's own limit on a label name.
MAX_LABEL_NAME = 50

READINESS = "readiness"
PRIORITY = "priority"
TYPE = "type"
AREA = "area"
HIERARCHY = "hierarchy"

_EPIC = "epic"
_INITIATIVE = "initiative"
_FREE_PREFIXES = "free_prefixes"
_KNOWN_KEYS = (READINESS, PRIORITY, TYPE, "areas", _EPIC, _INITIATIVE, _FREE_PREFIXES)
_ENTRY_KEYS = ("name", "aliases")


@dataclass(frozen=True)
class Canonical:
    """One canonical label value and the existing labels that mean the same thing."""

    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabelSchema:
    """A repo's label schema. ``errors`` lists what ``parse_labels_yml`` couldn't use."""

    readiness: tuple[Canonical, ...]
    priority: tuple[Canonical, ...]
    type: tuple[Canonical, ...]
    area_prefix: str = "area:"
    areas: tuple[Canonical, ...] = ()
    free_prefixes: tuple[str, ...] = ("review:", "campaign:", "theme:")
    epic: Canonical = Canonical(_EPIC)
    initiative: Canonical = Canonical(_INITIATIVE)
    errors: tuple[str, ...] = ()


def _values(*names: str) -> tuple[Canonical, ...]:
    return tuple(Canonical(name) for name in names)


DEFAULT_SCHEMA = LabelSchema(
    readiness=_values(
        "agent-ready", "human-needed", "information-needed", "design-needed", "blocked", "icebox"
    ),
    priority=_values("priority: high", "priority: medium", "priority: low"),
    type=_values(
        "bug", "enhancement", "tech-debt", "test", "performance", "security", "documentation"
    ),
)


@dataclass(frozen=True)
class Conflict:
    """More than one value in a dimension that allows one (or both hierarchy labels)."""

    dimension: str
    values: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.dimension}: {', '.join(self.values)}"


@dataclass(frozen=True)
class Classified:
    """An issue's labels read against a schema, aliases mapped to their canonical values.

    A conflicted dimension's value is ``None`` and the conflict is in ``conflicts``.
    ``areas`` holds schema areas by canonical name and any other ``area:`` label as it is.
    ``unknown`` is every label outside the schema and the free-form prefixes.
    """

    readiness: str | None
    priority: str | None
    type: str | None
    areas: list[str]
    unknown: list[str]
    conflicts: list[Conflict] = field(default_factory=list)
    is_epic: bool = False
    is_initiative: bool = False


@dataclass(frozen=True)
class _Index:
    by_label: Mapping[str, tuple[str, str]]  # casefolded label -> (dimension, canonical)
    area_prefix: str
    free_prefixes: tuple[str, ...]


@lru_cache(maxsize=32)
def _index(schema: LabelSchema) -> _Index:
    by_label: dict[str, tuple[str, str]] = {}
    dimensions: tuple[tuple[str, Sequence[Canonical]], ...] = (
        (READINESS, schema.readiness),
        (PRIORITY, schema.priority),
        (TYPE, schema.type),
        (AREA, schema.areas),
        (HIERARCHY, (schema.epic, schema.initiative)),
    )
    for dimension, values in dimensions:
        for value in values:
            for label in (value.name, *value.aliases):
                by_label.setdefault(label.casefold(), (dimension, value.name))
    return _Index(
        by_label,
        schema.area_prefix.casefold(),
        tuple(prefix.casefold() for prefix in schema.free_prefixes),
    )


def _add(values: list[str], value: str) -> None:
    if all(existing.casefold() != value.casefold() for existing in values):
        values.append(value)


def classify(labels: Iterable[str], schema: LabelSchema) -> Classified:
    """Read an issue's ``labels`` against ``schema``.

    Aliases count as their canonical value. Two values in an exactly-1 dimension (or both the
    epic and initiative labels) are reported in ``conflicts``, never resolved by guessing.
    """
    index = _index(schema)
    found: dict[str, list[str]] = {READINESS: [], PRIORITY: [], TYPE: [], AREA: [], HIERARCHY: []}
    unknown: list[str] = []
    for label in labels:
        key = label.casefold()
        if key in index.by_label:
            dimension, canonical = index.by_label[key]
            _add(found[dimension], canonical)
        elif key.startswith(index.area_prefix):
            _add(found[AREA], label)
        elif not key.startswith(index.free_prefixes):
            _add(unknown, label)
    single: dict[str, str | None] = {}
    conflicts: list[Conflict] = []
    for dimension in (READINESS, PRIORITY, TYPE, HIERARCHY):
        values = found[dimension]
        single[dimension] = values[0] if len(values) == 1 else None
        if len(values) > 1:
            conflicts.append(Conflict(dimension, tuple(sorted(values))))
    hierarchy = found[HIERARCHY]
    return Classified(
        readiness=single[READINESS],
        priority=single[PRIORITY],
        type=single[TYPE],
        areas=found[AREA],
        unknown=unknown,
        conflicts=conflicts,
        is_epic=schema.epic.name in hierarchy,
        is_initiative=schema.initiative.name in hierarchy,
    )


def write_name(canonical: Canonical, existing_labels: Iterable[str]) -> str | None:
    """The label name to write for ``canonical`` on a repo whose labels are ``existing_labels``.

    Until the repo is migrated, an alias that exists on GitHub wins (spec §6.2), in GitHub's
    own spelling; then the canonical name if it exists. ``None`` means neither exists: Jesinia
    never creates a label implicitly, so the caller fails that step (or offers the setup batch).

    A schema overlay change can change which labels count, so callers that store completeness
    must recompute it when the schema changes (Issues sync does, Task 3).
    """
    existing: dict[str, str] = {}
    for label in existing_labels:
        existing.setdefault(label.casefold(), label)
    for alias in canonical.aliases:
        if alias.casefold() in existing:
            return existing[alias.casefold()]
    return existing.get(canonical.name.casefold())


def _nesting_error(text: str) -> str | None:
    """Refuse flow collections nested deeper than ``MAX_LABELS_YML_NESTING``; quoted strings
    and comments don't count."""
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
            if depth > MAX_LABELS_YML_NESTING:
                return f"labels.yml nests deeper than {MAX_LABELS_YML_NESTING} levels"
        elif char in "]}":
            depth -= 1
        previous = char
    return None


def _label_name(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name if 0 < len(name) <= MAX_LABEL_NAME else None


class _Parser:
    """Builds a schema from a parsed ``labels.yml`` mapping, listing what it can't use.

    Every label name and alias may appear once across the whole schema; later repeats are
    dropped with an error, so a label never means two things.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self._seen: set[str] = set()

    def _admit(self, where: str, label: str) -> bool:
        if label.casefold() in self._seen:
            self.errors.append(f"{where}: {label!r} is already in the schema")
            return False
        self._seen.add(label.casefold())
        return True

    def _canonical(
        self, where: str, name: object, aliases: object, prefix: str | None
    ) -> Canonical | None:
        label = _label_name(name)
        if label is None:
            self.errors.append(f"{where}: the label name must be 1-{MAX_LABEL_NAME} characters")
            return None
        if prefix is not None and not label.casefold().startswith(prefix):
            self.errors.append(f"{where}: {label!r} must start with {prefix!r}")
            return None
        if not self._admit(where, label):
            return None
        if not isinstance(aliases, list):
            self.errors.append(f"{where}: aliases must be a list")
            aliases = []
        if len(aliases) > MAX_ALIASES:
            self.errors.append(f"{where}: more than {MAX_ALIASES} aliases, the rest are ignored")
            aliases = aliases[:MAX_ALIASES]
        kept: list[str] = []
        for raw in aliases:
            alias = _label_name(raw)
            if alias is None:
                self.errors.append(f"{where}: alias {raw!r} must be a label name")
            elif self._admit(where, alias):
                kept.append(alias)
        return Canonical(label, tuple(kept))

    def entry(self, where: str, raw: object, prefix: str | None) -> Canonical | None:
        if isinstance(raw, str):
            return self._canonical(where, raw, [], prefix)
        if not isinstance(raw, dict):
            self.errors.append(f"{where}: expected a label name or a mapping with a name")
            return None
        canonical = self._canonical(where, raw.get("name"), raw.get("aliases", []), prefix)
        self.errors.extend(f"{where}: unknown key {key!r}" for key in raw if key not in _ENTRY_KEYS)
        return canonical

    def _defaults(self, key: str, default: tuple[Canonical, ...]) -> tuple[Canonical, ...]:
        kept = (self._canonical(key, value.name, list(value.aliases), None) for value in default)
        return tuple(value for value in kept if value is not None)

    def dimension(
        self,
        data: Mapping[object, object],
        key: str,
        default: tuple[Canonical, ...],
        prefix: str | None = None,
    ) -> tuple[Canonical, ...]:
        if key not in data:
            return self._defaults(key, default)
        raw = data[key]
        if not isinstance(raw, list):
            self.errors.append(f"{key}: expected a list")
            return self._defaults(key, default)
        if len(raw) > MAX_ENTRIES:
            self.errors.append(f"{key}: more than {MAX_ENTRIES} entries, the rest are ignored")
            raw = raw[:MAX_ENTRIES]
        entries = (self.entry(f"{key}[{i}]", item, prefix) for i, item in enumerate(raw, 1))
        values = tuple(value for value in entries if value is not None)
        if not values and default:
            self.errors.append(f"{key}: no usable labels, the default is used")
            return self._defaults(key, default)
        return values

    def hierarchy(self, data: Mapping[object, object], key: str, default: Canonical) -> Canonical:
        if key in data and (value := self.entry(key, data[key], None)) is not None:
            return value
        self._admit(key, default.name)  # a repeat is listed; the default label still applies
        return default

    def free_prefixes(
        self, data: Mapping[object, object], default: tuple[str, ...]
    ) -> tuple[str, ...]:
        raw = data.get(_FREE_PREFIXES, [])
        if not isinstance(raw, list):
            self.errors.append(f"{_FREE_PREFIXES}: expected a list")
            return default
        prefixes = list(default)
        for i, item in enumerate(raw, 1):
            prefix = _label_name(item)
            if prefix is None or not prefix.endswith(":"):
                self.errors.append(
                    f"{_FREE_PREFIXES}[{i}]: {item!r} must be a prefix ending in ':'"
                )
            else:
                _add(prefixes, prefix)
        return tuple(prefixes)


def _fallback(error: str) -> LabelSchema:
    return replace(DEFAULT_SCHEMA, errors=(error,))


def parse_labels_yml(text: str) -> LabelSchema:
    """Parse a repo's ``.jesinia/labels.yml`` leniently: never raises.

    An unusable document (too large, nested too deep, not YAML, not a mapping) gives
    ``DEFAULT_SCHEMA`` with one error. Otherwise each bad key or entry is listed in ``errors``
    and skipped; a dimension that is missing, not a list or has no usable labels keeps the
    default.
    """
    if len(text.encode("utf-8", errors="replace")) > MAX_LABELS_YML_BYTES:
        return _fallback(f"labels.yml is over {MAX_LABELS_YML_BYTES // 1024} KB")
    if (nesting := _nesting_error(text)) is not None:
        return _fallback(nesting)
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        # RecursionError and ValueError: what a parser raises on hostile input it didn't
        # anticipate; the nesting check above should stop the deep cases first.
        return _fallback(f"labels.yml is not valid YAML: {exc}")
    if data is None:
        return _fallback("labels.yml is empty, the default schema is used")
    if not isinstance(data, dict):
        return _fallback("labels.yml must be a mapping, the default schema is used")
    parser = _Parser()
    default = DEFAULT_SCHEMA
    readiness = parser.dimension(data, READINESS, default.readiness)
    priority = parser.dimension(data, PRIORITY, default.priority)
    type_ = parser.dimension(data, TYPE, default.type)
    areas = parser.dimension(data, "areas", default.areas, default.area_prefix)
    epic = parser.hierarchy(data, _EPIC, default.epic)
    initiative = parser.hierarchy(data, _INITIATIVE, default.initiative)
    free_prefixes = parser.free_prefixes(data, default.free_prefixes)
    parser.errors.extend(f"unknown key {key!r}" for key in data if key not in _KNOWN_KEYS)
    return LabelSchema(
        readiness=readiness,
        priority=priority,
        type=type_,
        areas=areas,
        free_prefixes=free_prefixes,
        epic=epic,
        initiative=initiative,
        errors=tuple(parser.errors),
    )
