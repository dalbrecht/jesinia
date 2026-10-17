# SPDX-License-Identifier: AGPL-3.0-or-later
"""Completeness (spec §2, §6.2): every exactly-1 dimension has one value and there is at least
one area; epics and initiatives need priority only."""

from __future__ import annotations

from jesinia.apps.issues.domain.completeness import Completeness, completeness
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA, classify, parse_labels_yml

AMICUS = """\
readiness:
  - agent-ready
  - name: design-needed
    aliases: [design needed]
areas:
  - name: area:apple
    aliases: [platform:ios, apple]
"""

FULL = ["agent-ready", "priority: low", "bug", "area:kb"]


def test_a_full_label_set_is_complete() -> None:
    got = completeness(classify(FULL, DEFAULT_SCHEMA), is_epic=False)
    assert got == Completeness(complete=True, missing=[], conflicts=[])


def test_missing_dimensions_are_listed_in_order() -> None:
    got = completeness(classify(["rust"], DEFAULT_SCHEMA), is_epic=False)
    assert got == Completeness(
        complete=False, missing=["readiness", "priority", "type", "area"], conflicts=[]
    )


def test_aliases_count_toward_completeness() -> None:
    schema = parse_labels_yml(AMICUS)
    got = completeness(
        classify(["design needed", "priority: high", "enhancement", "apple"], schema),
        is_epic=False,
    )
    assert got.complete and got.missing == [] and got.conflicts == []


def test_conflicting_readiness_is_reported() -> None:
    got = completeness(
        classify(["agent-ready", "blocked", "priority: low", "bug", "area:kb"], DEFAULT_SCHEMA),
        is_epic=False,
    )
    assert got == Completeness(
        complete=False, missing=[], conflicts=["readiness: agent-ready, blocked"]
    )
    two_priorities = classify([*FULL, "priority: high"], DEFAULT_SCHEMA)
    assert two_priorities.priority is None
    assert completeness(two_priorities, is_epic=False).conflicts == [
        "priority: priority: high, priority: low"
    ]


def test_epic_needs_priority_only() -> None:
    assert completeness(classify(["priority: high"], DEFAULT_SCHEMA), is_epic=True).complete
    got = completeness(classify(["bug", "area:kb"], DEFAULT_SCHEMA), is_epic=True)
    assert got == Completeness(complete=False, missing=["priority"], conflicts=[])
    # The epic label makes it an epic even when the caller saw no sub-issues.
    assert completeness(classify(["epic", "priority: low"], DEFAULT_SCHEMA), is_epic=False).complete
    # Only the dimensions an epic needs can conflict.
    mixed = classify(["agent-ready", "blocked", "priority: low"], DEFAULT_SCHEMA)
    assert completeness(mixed, is_epic=True).complete


def test_initiative_needs_priority_only() -> None:
    initiative = classify(["initiative", "priority: medium"], DEFAULT_SCHEMA)
    assert completeness(initiative, is_epic=False).complete
    bare = classify(["initiative"], DEFAULT_SCHEMA)
    assert completeness(bare, is_epic=False).missing == ["priority"]


def test_epic_and_initiative_labels_together_conflict() -> None:
    got = completeness(
        classify(["epic", "initiative", "priority: low"], DEFAULT_SCHEMA), is_epic=True
    )
    assert got == Completeness(
        complete=False, missing=[], conflicts=["hierarchy: epic, initiative"]
    )
