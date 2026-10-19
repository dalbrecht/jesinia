# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-repo label schema (spec §2, §6.2).

A schema lists, per dimension, the canonical label values and the existing GitHub labels that
mean the same thing (aliases). It comes from the repo's ``.jesinia/labels.yml`` when present,
otherwise from Jesinia's settings, starting from ``DEFAULT_SCHEMA``.

``.jesinia/labels.yml`` is untrusted repo content, so ``parse_labels_yml`` is lenient: it never
raises; it caps the document's size, refuses YAML anchors and aliases (they share one object
across many references, so anything that walks or prints the result would expand them) and
nesting deeper than ``MAX_LABELS_YML_NESTING`` while composing, and every unusable key or entry
becomes a line in ``LabelSchema.errors`` while the rest of the schema loads. Error messages
quote parsed values only as a type name or a short, cut repr. The format::

    readiness:            # likewise priority and type; a missing one keeps the default
      - agent-ready
      - name: design-needed
        aliases: [design needed]
    areas:                # names start with "area:"; aliases may be any existing label
      - name: area:apple
        aliases: [platform:ios, apple]
    epic: epic            # or {name: epic, aliases: [...]}; initiative likewise
    free_prefixes: ["ops:"]   # added to review:, campaign: and theme:

Labels match case-insensitively and with surrounding whitespace ignored, as GitHub's label
names do.

An initiative, like an epic, needs priority only: spec §6.2 says "`initiative` sits above
epics, with the same rule and its own label", and the plan's Global Constraints place it beside
the epic completeness rule; no other completeness rule for initiatives exists.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Literal, get_args

import yaml

MAX_LABELS_YML_BYTES = 64 * 1024
# Collections nested deeper than this are refused while composing, before the recursive
# composer nears Python's recursion limit.
MAX_LABELS_YML_NESTING = 32
# How much of a parsed string an error message quotes.
MAX_QUOTED = 80
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


def _dimensions(schema: LabelSchema) -> tuple[tuple[str, Sequence[Canonical]], ...]:
    return (
        (READINESS, schema.readiness),
        (PRIORITY, schema.priority),
        (TYPE, schema.type),
        (AREA, schema.areas),
        (HIERARCHY, (schema.epic, schema.initiative)),
    )


@lru_cache(maxsize=32)
def _index(schema: LabelSchema) -> _Index:
    by_label: dict[str, tuple[str, str]] = {}
    for dimension, values in _dimensions(schema):
        for value in values:
            for label in (value.name, *value.aliases):
                by_label.setdefault(_key(label), (dimension, value.name))
    return _Index(
        by_label,
        schema.area_prefix.casefold(),
        tuple(prefix.casefold() for prefix in schema.free_prefixes),
    )


def label_key(label: str) -> str:
    """How label names compare: Unicode NFC, surrounding whitespace ignored, casefolded (as
    GitHub's own names compare, near enough)."""
    return unicodedata.normalize("NFC", label).strip().casefold()


_key = label_key


def _add(values: list[str], value: str) -> None:
    if all(_key(existing) != _key(value) for existing in values):
        values.append(value)


def unmapped_labels(labels: Iterable[str], schema: LabelSchema) -> list[str]:
    """The ``labels`` (a repo's, as GitHub spells them) that mean nothing to ``schema``: neither
    a canonical value nor an alias, nor an area by its prefix, nor a free-form prefix. Each once,
    in the order given; Settings offers them as aliases."""
    return classify(labels, schema).unknown


def classify(labels: Iterable[str], schema: LabelSchema) -> Classified:
    """Read an issue's ``labels`` against ``schema``.

    Aliases count as their canonical value. Two values in an exactly-1 dimension (or both the
    epic and initiative labels) are reported in ``conflicts``, never resolved by guessing.
    """
    index = _index(schema)
    found: dict[str, list[str]] = {READINESS: [], PRIORITY: [], TYPE: [], AREA: [], HIERARCHY: []}
    unknown: list[str] = []
    for raw in labels:
        label = raw.strip()
        key = label_key(raw)
        if not key:
            continue
        if key in index.by_label:
            dimension, canonical = index.by_label[key]
            _add(found[dimension], canonical)
        elif key.startswith(index.area_prefix) and len(key) > len(index.area_prefix):
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
        existing.setdefault(_key(label), label.strip())
    for alias in canonical.aliases:
        if _key(alias) in existing:
            return existing[_key(alias)]
    return existing.get(_key(canonical.name))


OverlayDimension = Literal["readiness", "priority", "type", "area"]
OVERLAY_DIMENSIONS: tuple[OverlayDimension, ...] = get_args(OverlayDimension)


@dataclass(frozen=True)
class SchemaEntry:
    """A schema value the author approved outside labels.yml and Settings (a ``new_area``
    suggestion, later a published ``theme:``), kept in the local ``schema_overlay``."""

    dimension: OverlayDimension
    canonical: str
    aliases: tuple[str, ...] = ()


def with_overlay(schema: LabelSchema, entries: Iterable[SchemaEntry]) -> LabelSchema:
    """``schema`` with the approved ``entries`` appended to their dimensions. It is merged over
    every schema read, a read-only labels.yml's included, so a later sync from the file never
    drops an approved entry. An entry whose name or an alias is already a label of the schema
    (by ``label_key``) is left out, so merging twice changes nothing and a label never means two
    things."""
    taken = {
        label_key(label)
        for _, values in _dimensions(schema)
        for value in values
        for label in (value.name, *value.aliases)
    }
    added: dict[str, list[Canonical]] = {dimension: [] for dimension in OVERLAY_DIMENSIONS}
    for entry in entries:
        labels = [label_key(label) for label in (entry.canonical, *entry.aliases)]
        if any(label in taken for label in labels):
            continue
        taken.update(labels)
        added[entry.dimension].append(Canonical(entry.canonical, entry.aliases))
    if not any(added.values()):
        return schema
    return replace(
        schema,
        readiness=(*schema.readiness, *added[READINESS]),
        priority=(*schema.priority, *added[PRIORITY]),
        type=(*schema.type, *added[TYPE]),
        areas=(*schema.areas, *added[AREA]),
    )


class _LabelsYmlRefusedError(ValueError):
    """A ``labels.yml`` the composer refuses outright (an anchor or alias, or too deep)."""


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses anchors and aliases at the first one, and refuses nesting
    deeper than ``MAX_LABELS_YML_NESTING`` before recursing into it."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        # check_event parses the next event into current_event without consuming it.
        is_alias = self.check_event(yaml.AliasEvent)
        anchor: object = getattr(self.current_event, "anchor", None)
        if is_alias or anchor is not None:
            raise _LabelsYmlRefusedError(
                "labels.yml uses YAML anchors or aliases, which are not allowed"
            )
        if self._depth >= MAX_LABELS_YML_NESTING:
            raise _LabelsYmlRefusedError(
                f"labels.yml nests deeper than {MAX_LABELS_YML_NESTING} levels"
            )
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _load(text: str) -> object:
    loader = _StrictLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _describe(value: object) -> str:
    """A parsed value as an error message may quote it: a type name, or a string's repr cut to
    ``MAX_QUOTED`` characters, so a hostile value can't inflate the errors."""
    if not isinstance(value, str):
        return type(value).__name__
    quoted = repr(value[: MAX_QUOTED + 1])
    return quoted if len(quoted) <= MAX_QUOTED else quoted[: MAX_QUOTED - 1] + "…"


def _label_name(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name if 0 < len(name) <= MAX_LABEL_NAME else None


class _Parser:
    """Builds a schema from a parsed ``labels.yml`` mapping, listing what it can't use.

    Every label name and alias may appear once across the whole schema (compared by
    ``label_key``); later repeats are dropped with an error, so a label never means two things.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self._seen: set[str] = set()

    def _admit(self, where: str, label: str) -> bool:
        # ``label_key``: an alias equal to a name in another Unicode form is the same label,
        # and would otherwise make a migration move a label onto itself and delete it.
        if label_key(label) in self._seen:
            self.errors.append(f"{where}: {label!r} is already in the schema")
            return False
        self._seen.add(label_key(label))
        return True

    def _canonical(
        self, where: str, name: object, aliases: object, prefix: str | None
    ) -> Canonical | None:
        label = _label_name(name)
        if label is None:
            self.errors.append(f"{where}: the label name must be 1-{MAX_LABEL_NAME} characters")
            return None
        if prefix is not None and (
            not label.casefold().startswith(prefix) or len(label) == len(prefix)
        ):
            self.errors.append(f"{where}: {label!r} must be {prefix!r} followed by a name")
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
                self.errors.append(f"{where}: alias must be a label name, got {_describe(raw)}")
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
        self.errors.extend(
            f"{where}: unknown key {_describe(key)}" for key in raw if key not in _ENTRY_KEYS
        )
        return canonical

    def _defaults(self, key: str, default: tuple[Canonical, ...]) -> tuple[Canonical, ...]:
        kept = (self._canonical(key, value.name, list(value.aliases), None) for value in default)
        return tuple(value for value in kept if value is not None)

    def dimension(
        self,
        data: Mapping[str, object],
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

    def hierarchy(self, data: Mapping[str, object], key: str, default: Canonical) -> Canonical:
        if key in data and (value := self.entry(key, data[key], None)) is not None:
            return value
        # Hierarchy labels are admitted first, so only the other hierarchy label can repeat this
        # one; the repeat is listed and the default still applies.
        self._admit(key, default.name)
        return default

    def free_prefixes(
        self, data: Mapping[str, object], default: tuple[str, ...]
    ) -> tuple[str, ...]:
        raw = data.get(_FREE_PREFIXES, [])
        if not isinstance(raw, list):
            self.errors.append(f"{_FREE_PREFIXES}: expected a list")
            return default
        if len(raw) > MAX_ENTRIES:
            self.errors.append(
                f"{_FREE_PREFIXES}: more than {MAX_ENTRIES} entries, the rest are ignored"
            )
            raw = raw[:MAX_ENTRIES]
        prefixes = list(default)
        for i, item in enumerate(raw, 1):
            prefix = _label_name(item)
            if prefix is None or not prefix.endswith(":"):
                self.errors.append(
                    f"{_FREE_PREFIXES}[{i}]: must be a prefix ending in ':', got {_describe(item)}"
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
    try:
        data = _load(text)
    except _LabelsYmlRefusedError as exc:
        return _fallback(str(exc))
    except (yaml.YAMLError, ValueError) as exc:
        # ValueError: what a constructor raises on a malformed scalar it didn't anticipate.
        return _fallback(f"labels.yml is not valid YAML: {exc}")
    if data is None:
        return _fallback("labels.yml is empty, the default schema is used")
    if not isinstance(data, dict):
        return _fallback("labels.yml must be a mapping, the default schema is used")
    return schema_from_mapping(data)


def schema_from_mapping(data: Mapping[str, object]) -> LabelSchema:
    """Build a schema from a parsed ``labels.yml``-shaped mapping, leniently: never raises.
    (A YAML mapping may hold keys that aren't strings; they only ever miss the lookups and are
    listed as unknown.)
    Each bad key or entry is listed in ``errors`` and skipped; a dimension that is missing, not
    a list or has no usable labels keeps the default."""
    parser = _Parser()
    default = DEFAULT_SCHEMA
    # The hierarchy labels first, so a dimension's alias can't silently take them over.
    epic = parser.hierarchy(data, _EPIC, default.epic)
    initiative = parser.hierarchy(data, _INITIATIVE, default.initiative)
    readiness = parser.dimension(data, READINESS, default.readiness)
    priority = parser.dimension(data, PRIORITY, default.priority)
    type_ = parser.dimension(data, TYPE, default.type)
    areas = parser.dimension(data, "areas", default.areas, default.area_prefix)
    free_prefixes = parser.free_prefixes(data, default.free_prefixes)
    parser.errors.extend(f"unknown key {_describe(key)}" for key in data if key not in _KNOWN_KEYS)
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


class InvalidSettingsSchemaError(ValueError):
    """A schema made in Settings that breaks the schema rules; ``errors`` lists every
    problem. Settings refuse what ``labels.yml`` would skip: the author is there to fix it."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("the label schema has problems: " + "; ".join(errors))
        self.errors = tuple(errors)


def validate_settings_schema(data: Mapping[str, object]) -> LabelSchema:
    """The schema a Settings edit describes (``labels.yml``'s shape and rules).

    Raises:
        InvalidSettingsSchemaError: any entry or key is unusable.
    """
    schema = schema_from_mapping(data)
    if schema.errors:
        raise InvalidSettingsSchemaError(schema.errors)
    return schema
