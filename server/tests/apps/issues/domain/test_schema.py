# SPDX-License-Identifier: AGPL-3.0-or-later
"""The label schema (spec §2): canonical values and aliases per dimension, the lenient
``.jesinia/labels.yml`` parser, and the label name Jesinia writes."""

from __future__ import annotations

import time
import tracemalloc

import pytest

from jesinia.apps.issues.domain.schema import (
    DEFAULT_SCHEMA,
    MAX_LABELS_YML_BYTES,
    MAX_LABELS_YML_NESTING,
    Canonical,
    Conflict,
    classify,
    parse_labels_yml,
    write_name,
)

AMICUS = """\
readiness:
  - agent-ready
  - human-needed
  - information-needed
  - name: design-needed
    aliases: [design needed]
  - blocked
  - icebox
areas:
  - name: area:apple
    aliases: [platform:ios, apple]
  - name: area:wiki
    aliases: [wiki]
"""


def test_default_schema_matches_the_convention() -> None:
    assert [c.name for c in DEFAULT_SCHEMA.readiness] == [
        "agent-ready",
        "human-needed",
        "information-needed",
        "design-needed",
        "blocked",
        "icebox",
    ]
    assert [c.name for c in DEFAULT_SCHEMA.priority] == [
        "priority: high",
        "priority: medium",
        "priority: low",
    ]
    assert [c.name for c in DEFAULT_SCHEMA.type] == [
        "bug",
        "enhancement",
        "tech-debt",
        "test",
        "performance",
        "security",
        "documentation",
    ]
    assert DEFAULT_SCHEMA.area_prefix == "area:"
    assert DEFAULT_SCHEMA.areas == ()
    assert DEFAULT_SCHEMA.free_prefixes == ("review:", "campaign:", "theme:")
    assert DEFAULT_SCHEMA.epic == Canonical("epic")
    assert DEFAULT_SCHEMA.initiative == Canonical("initiative")
    assert DEFAULT_SCHEMA.errors == ()


def test_classify_maps_aliases_to_canonical_values() -> None:
    schema = parse_labels_yml(AMICUS)
    assert schema.errors == ()
    got = classify(
        ["Design Needed", "priority: high", "bug", "apple", "wiki", "review:security", "rust"],
        schema,
    )
    assert got.readiness == "design-needed"
    assert got.priority == "priority: high"
    assert got.type == "bug"
    assert got.areas == ["area:apple", "area:wiki"]
    assert got.unknown == ["rust"]
    assert got.conflicts == []
    assert not got.is_epic and not got.is_initiative


def test_areas_outside_the_schema_still_count_by_prefix() -> None:
    got = classify(["area:new", "area:new", "AREA:Other"], DEFAULT_SCHEMA)
    assert got.areas == ["area:new", "AREA:Other"]
    assert got.unknown == []


def test_a_bare_area_prefix_is_not_an_area() -> None:
    got = classify(["area:", " Area: "], DEFAULT_SCHEMA)
    assert got.areas == [] and got.unknown == ["area:"]
    schema = parse_labels_yml("areas: ['area:']\n")
    assert schema.areas == ()
    assert schema.errors == ("areas[1]: 'area:' must be 'area:' followed by a name",)


def test_classify_strips_and_casefolds_like_alias_matching() -> None:
    schema = parse_labels_yml(AMICUS)
    got = classify([" Agent-Ready ", "  ", " APPLE", " rust "], schema)
    assert got.readiness == "agent-ready"
    assert got.areas == ["area:apple"]
    assert got.unknown == ["rust"]
    assert write_name(Canonical("bug"), [" Bug "]) == "Bug"


def test_hierarchy_labels_cannot_be_taken_by_another_dimension() -> None:
    schema = parse_labels_yml("readiness:\n  - name: agent-ready\n    aliases: [Epic]\n")
    assert schema.readiness[0].aliases == ()
    assert schema.errors == ("readiness[1]: 'Epic' is already in the schema",)
    assert classify(["epic"], schema).is_epic


def test_alias_and_canonical_of_one_value_are_not_a_conflict() -> None:
    schema = parse_labels_yml(AMICUS)
    got = classify(["design needed", "design-needed", "platform:ios", "area:apple"], schema)
    assert got.readiness == "design-needed" and got.conflicts == []
    assert got.areas == ["area:apple"]


def test_hierarchy_labels_are_recognised() -> None:
    got = classify(["Epic"], DEFAULT_SCHEMA)
    assert got.is_epic and not got.is_initiative and got.unknown == []
    got = classify(["initiative"], DEFAULT_SCHEMA)
    assert got.is_initiative and not got.is_epic
    both = classify(["epic", "initiative"], DEFAULT_SCHEMA)
    assert both.conflicts == [Conflict("hierarchy", ("epic", "initiative"))]
    assert both.conflicts[0].describe() == "hierarchy: epic, initiative"


def test_labels_yml_parse_is_lenient() -> None:
    schema = parse_labels_yml(
        """\
readiness:
  - agent-ready
  - 42
  - name: ""
  - name: blocked
    aliases: not-a-list
  - name: icebox
    aliases: [frozen, 7]
  - {aliases: [nameless]}
priority: not-a-list
type: []
areas:
  - wiki
  - area:wiki
  - area:wiki
  - name: area:apple
    aliases: [frozen]
    colour: red
epic: {name: epic, aliases: [big]}
initiative: 3
free_prefixes: [ "ops:", 9, "noprefix" ]
colour: blue
"""
    )
    # Every bad entry is listed and skipped; the rest of the schema still loads.
    assert [c.name for c in schema.readiness] == ["agent-ready", "blocked", "icebox"]
    assert schema.readiness[1].aliases == ()
    assert schema.readiness[2].aliases == ("frozen",)
    # A dimension that isn't a list keeps the default; an empty one does too.
    assert schema.priority == DEFAULT_SCHEMA.priority
    assert schema.type == DEFAULT_SCHEMA.type
    assert [c.name for c in schema.areas] == ["area:wiki", "area:apple"]
    assert schema.areas[1].aliases == ()
    assert schema.epic == Canonical("epic", ("big",))
    assert schema.initiative == DEFAULT_SCHEMA.initiative
    assert schema.free_prefixes == ("review:", "campaign:", "theme:", "ops:")
    assert schema.errors == (
        # The hierarchy labels are admitted first, so no dimension can take them over.
        "initiative: expected a label name or a mapping with a name",
        "readiness[2]: expected a label name or a mapping with a name",
        "readiness[3]: the label name must be 1-50 characters",
        "readiness[4]: aliases must be a list",
        "readiness[5]: alias must be a label name, got int",
        "readiness[6]: the label name must be 1-50 characters",
        "priority: expected a list",
        "type: no usable labels, the default is used",
        "areas[1]: 'wiki' must be 'area:' followed by a name",
        "areas[3]: 'area:wiki' is already in the schema",
        "areas[4]: 'frozen' is already in the schema",
        "areas[4]: unknown key 'colour'",
        "free_prefixes[2]: must be a prefix ending in ':', got int",
        "free_prefixes[3]: must be a prefix ending in ':', got 'noprefix'",
        "unknown key 'colour'",
    )


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "labels.yml is empty, the default schema is used"),
        ("- a\n- b\n", "labels.yml must be a mapping, the default schema is used"),
        ("readiness: [unclosed\n", "labels.yml is not valid YAML"),
        ("a: \x00\n", "labels.yml is not valid YAML"),
    ],
)
def test_unusable_labels_yml_falls_back_to_the_default(text: str, error: str) -> None:
    schema = parse_labels_yml(text)
    assert schema.readiness == DEFAULT_SCHEMA.readiness
    assert len(schema.errors) == 1 and schema.errors[0].startswith(error)


def test_labels_yml_is_size_capped_and_depth_guarded() -> None:
    big = parse_labels_yml("#" * (MAX_LABELS_YML_BYTES + 1))
    assert big.errors == (f"labels.yml is over {MAX_LABELS_YML_BYTES // 1024} KB",)
    deep = parse_labels_yml("a: " + "[" * (MAX_LABELS_YML_NESTING + 1))
    assert deep.errors == (f"labels.yml nests deeper than {MAX_LABELS_YML_NESTING} levels",)
    # Brackets in quotes and comments don't count.
    ok = parse_labels_yml("readiness: ['" + "[" * 40 + '\', "\\"[", ready]  # ' + "{" * 40 + "\n")
    assert ok.errors == ()
    assert [c.name for c in ok.readiness] == ["[" * 40, '"[', "ready"]


def test_labels_yml_entry_and_alias_counts_are_capped() -> None:
    many = "readiness:\n" + "".join(f"  - r{i}\n" for i in range(300))
    schema = parse_labels_yml(many)
    assert len(schema.readiness) == 200
    assert schema.errors == ("readiness: more than 200 entries, the rest are ignored",)
    aliases = ", ".join(f"a{i}" for i in range(60))
    schema = parse_labels_yml(f"readiness:\n  - name: r\n    aliases: [{aliases}]\n")
    assert len(schema.readiness[0].aliases) == 50
    assert schema.errors == ("readiness[1]: more than 50 aliases, the rest are ignored",)


def test_free_prefixes_are_capped() -> None:
    many = "free_prefixes:\n" + "".join(f"  - 'p{i}:'\n" for i in range(300))
    schema = parse_labels_yml(many)
    assert len(schema.free_prefixes) == 3 + 200
    assert schema.errors == ("free_prefixes: more than 200 entries, the rest are ignored",)


def test_yaml_anchors_and_aliases_are_refused() -> None:
    laughs = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    laughs += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, 9)]
    nested = "\n".join(laughs) + "\nreadiness:\n  - name: r\n    aliases: *a8\n"
    repeated = "s: &s " + "x" * 30_000 + "\nfree_prefixes: [" + ", ".join(["*s"] * 3000) + "]\n"
    lone_anchor = "readiness: &r [agent-ready]\n"
    for text in (nested, repeated, lone_anchor):
        tracemalloc.start()
        started = time.perf_counter()
        schema = parse_labels_yml(text)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert schema.errors == ("labels.yml uses YAML anchors or aliases, which are not allowed",)
        assert schema.readiness == DEFAULT_SCHEMA.readiness
        assert elapsed < 2 and peak < 20 * 1024 * 1024


def test_bad_values_are_bounded_in_errors() -> None:
    long = "y" * 10_000
    schema = parse_labels_yml(
        f"readiness:\n  - name: r\n    aliases: [{long}, {{a: 1}}]\n{long[:200]}: 1\n"
    )
    assert schema.errors[0] == (f"readiness[1]: alias must be a label name, got '{'y' * 78}…")
    assert schema.errors[1] == "readiness[1]: alias must be a label name, got dict"
    assert schema.errors[2] == f"unknown key '{'y' * 78}…"
    assert max(len(error) for error in schema.errors) < 200


def test_block_nesting_is_depth_guarded() -> None:
    deep = "readiness:\n  " + "- " * (MAX_LABELS_YML_NESTING + 1) + "x\n"
    schema = parse_labels_yml(deep)
    assert schema.errors == (f"labels.yml nests deeper than {MAX_LABELS_YML_NESTING} levels",)
    very_deep = "readiness:\n  " + "- " * 20_000 + "x\n"
    assert parse_labels_yml(very_deep[:MAX_LABELS_YML_BYTES]).errors == schema.errors


def test_free_prefixes_must_be_a_list() -> None:
    schema = parse_labels_yml('free_prefixes: "ops:"\n')
    assert schema.free_prefixes == DEFAULT_SCHEMA.free_prefixes
    assert schema.errors == ("free_prefixes: expected a list",)


def test_write_name_prefers_existing_alias_until_migrated() -> None:
    design = Canonical("design-needed", ("design needed", "needs design"))
    # Not migrated: the alias that exists on GitHub is written, in GitHub's spelling.
    assert write_name(design, ["bug", "Design Needed"]) == "Design Needed"
    # Both exist (part-way through a migration): still the alias, until it's gone.
    assert write_name(design, ["design-needed", "needs design"]) == "needs design"
    # Migrated: the canonical name.
    assert write_name(design, ["DESIGN-needed"]) == "DESIGN-needed"
    # Neither exists: Jesinia never creates a label implicitly.
    assert write_name(design, ["bug"]) is None
