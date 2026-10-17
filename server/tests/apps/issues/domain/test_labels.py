# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label setup and migration plans (spec §2, §6.2): what an approved batch would create,
relabel and delete, and the checks an apply makes against its preview."""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jesinia.apps.issues.domain.labels import (
    PLAN_TTL,
    CreateLabel,
    DeleteLabel,
    InvalidLabelPlanKindError,
    LabelNameCollisionError,
    LabelPlan,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanOutdatedError,
    MigrationPlan,
    migration_aliases,
    parse_plan_kind,
    plan_migration,
    plan_setup,
    schema_labels,
)
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA, Canonical, LabelSchema, label_key

REPO = "acme/web"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SCHEMA = replace(
    DEFAULT_SCHEMA,
    readiness=(
        *(c for c in DEFAULT_SCHEMA.readiness if c.name != "design-needed"),
        Canonical("design-needed", ("design needed",)),
    ),
    areas=(Canonical("area:apple", ("platform:ios", "apple")), Canonical("area:wiki", ("wiki",))),
)


def all_names(schema: LabelSchema) -> list[str]:
    return [canonical.name for _, canonical in schema_labels(schema)]


def test_schema_labels_lists_every_canonical_in_dimension_order() -> None:
    names = all_names(SCHEMA)
    assert names[:2] == ["agent-ready", "human-needed"]
    assert names[-4:] == ["area:apple", "area:wiki", "epic", "initiative"]
    assert len(names) == 6 + 3 + 7 + 2 + 2


def test_setup_creates_only_labels_jesinia_could_not_write() -> None:
    existing = ["Agent-Ready", "priority: high", "bug", "design needed", "apple", "epic"]
    ops = plan_setup(SCHEMA, existing)
    names = [op.name for op in ops]
    # Present (any case), or present through an alias: not missing.
    present = {"agent-ready", "priority: high", "bug", "design-needed", "area:apple", "epic"}
    assert not present & set(names)
    assert names[:2] == ["human-needed", "information-needed"]
    assert "area:wiki" in names and "initiative" in names
    for op in ops:
        assert len(op.color) == 6 and op.description.endswith("(Jesinia label schema)")
    colors = {op.name: op.color for op in ops}
    assert colors["human-needed"] != colors["priority: low"] != colors["area:wiki"]


def test_setup_of_a_repo_with_every_label_is_empty() -> None:
    assert plan_setup(SCHEMA, all_names(SCHEMA)) == []


def test_migration_aliases_are_the_aliases_present_on_github() -> None:
    existing = ["design needed", "Apple", "platform:ios", "area:apple", "bug", "wiki-ish"]
    assert migration_aliases(SCHEMA, existing) == [
        ("design needed", "design-needed"),
        ("platform:ios", "area:apple"),
        ("Apple", "area:apple"),
    ]


def test_migration_creates_missing_canonicals_then_deletes_aliases_after_moving_members() -> None:
    existing = ["design needed", "Apple", "platform:ios", "area:apple", "bug"]
    members = {"design needed": [1], "platform:ios": [2], "Apple": [2, 4]}
    plan = plan_migration(SCHEMA, existing, members)
    # design-needed doesn't exist yet: created before anything is moved to it.
    assert [op.name for op in plan.create] == ["design-needed"]
    assert plan.delete == (
        DeleteLabel("design needed", "design-needed", (1,)),
        DeleteLabel("platform:ios", "area:apple", (2,)),
        DeleteLabel("Apple", "area:apple", (2, 4)),
    )
    assert plan.relabel == {1: ("design needed",), 2: ("platform:ios", "Apple"), 4: ("Apple",)}
    assert plan.ops() == [*plan.create, *plan.delete]


def test_migration_without_aliases_on_github_is_empty() -> None:
    plan = plan_migration(SCHEMA, ["bug", "area:apple"], {})
    assert plan == MigrationPlan((), ())
    assert plan.ops() == [] and plan.relabel == {}


def test_two_github_labels_with_one_key_are_refused_not_guessed() -> None:
    with pytest.raises(LabelNameCollisionError, match="Straße"):
        migration_aliases(SCHEMA, ["Straße", "STRASSE", "bug"])
    # NFC and NFD spellings of one name are the same label.
    nfd = unicodedata.normalize("NFD", "café")
    assert label_key(nfd) == label_key("Café ")
    with pytest.raises(LabelNameCollisionError):
        plan_setup(SCHEMA, [nfd, "café"])


def test_parse_plan_kind() -> None:
    assert parse_plan_kind("setup") == "setup"
    assert parse_plan_kind("migrate") == "migrate"
    with pytest.raises(InvalidLabelPlanKindError):
        parse_plan_kind("rename")


def plan() -> LabelPlan:
    return LabelPlan("p1", REPO, "setup", (CreateLabel("bug", "1d76db", "Type"),), T0)


def test_a_plan_expires_after_its_ttl() -> None:
    assert plan().expires_at == T0 + PLAN_TTL
    plan().check("setup", T0 + PLAN_TTL, plan().ops)
    with pytest.raises(LabelPlanExpiredError):
        plan().check("setup", T0 + PLAN_TTL + timedelta(seconds=1), plan().ops)


def test_a_plan_applies_only_as_its_own_kind() -> None:
    with pytest.raises(LabelPlanKindMismatchError, match="setup"):
        plan().check("migrate", T0, plan().ops)


def test_a_plan_whose_repo_state_changed_is_outdated() -> None:
    with pytest.raises(LabelPlanOutdatedError, match="preview"):
        plan().check("setup", T0, ())


def test_member_lists_never_make_a_plan_outdated() -> None:
    previewed = LabelPlan("p1", REPO, "migrate", (DeleteLabel("old", "new", (1, 2)),), T0)
    # Membership is listed again live at apply time, so only the shape is compared.
    previewed.check("migrate", T0, (DeleteLabel("old", "new", (1, 2, 3)),))
    with pytest.raises(LabelPlanOutdatedError):
        previewed.check("migrate", T0, (DeleteLabel("old", "newer", (1, 2)),))


def test_migration_never_plans_a_label_onto_itself() -> None:
    nfc = unicodedata.normalize("NFC", "area:café")
    nfd = unicodedata.normalize("NFD", "area:café")
    # Built directly: the parser refuses this schema, but a stored one must not do harm.
    schema = replace(DEFAULT_SCHEMA, areas=(Canonical(nfc, (nfd,)),))
    assert migration_aliases(schema, [nfc]) == []
    assert plan_migration(schema, [nfc], {nfc: [1, 2]}) == MigrationPlan((), ())
