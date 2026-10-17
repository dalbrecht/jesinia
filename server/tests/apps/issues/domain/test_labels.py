# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label setup and migration plans (spec §2, §6.2): what an approved batch would create,
relabel and delete, and the checks an apply makes against its preview."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jesinia.apps.issues.domain.labels import (
    PLAN_TTL,
    CreateLabel,
    DeleteLabel,
    InvalidLabelPlanKindError,
    LabelPlan,
    LabelPlanExpiredError,
    LabelPlanKindMismatchError,
    LabelPlanOutdatedError,
    MigrationPlan,
    Relabel,
    parse_plan_kind,
    plan_migration,
    plan_setup,
    schema_labels,
)
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA, Canonical, LabelSchema
from jesinia.services.github.domain.issue import Issue, IssueRef

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


def issue(n: int, *labels: str) -> Issue:
    return Issue(
        repo=REPO, number=n, id=1000 + n, title=f"Issue {n}", body="", state="open",
        state_reason=None, labels=labels, milestone=None, parent=None, sub_issues=(),
        created_at=T0, updated_at=T0, url=f"https://github.com/{REPO}/issues/{n}", author="o",
    )  # fmt: skip


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


def test_migration_maps_aliases_present_on_github_to_their_canonical() -> None:
    existing = ["design needed", "Apple", "platform:ios", "area:apple", "bug", "wiki-ish"]
    issues = [
        issue(1, "design needed", "bug"),
        issue(2, "apple", "platform:ios"),
        issue(3, "bug", "area:apple"),  # nothing to relabel
        issue(4, "area:apple", "Apple"),  # has the canonical too: the alias still goes
    ]
    plan = plan_migration(REPO, SCHEMA, existing, issues)
    # design-needed doesn't exist yet: created before anything is relabelled to it.
    assert plan.create == (
        CreateLabel("design-needed", plan.create[0].color, plan.create[0].description),
    )
    assert plan.relabel == {
        IssueRef(REPO, 1, 1001): Relabel(1, (("design needed", "design-needed"),)),
        IssueRef(REPO, 2, 1002): Relabel(
            2, (("apple", "area:apple"), ("platform:ios", "area:apple"))
        ),
        IssueRef(REPO, 4, 1004): Relabel(4, (("Apple", "area:apple"),)),
    }
    # Old labels in GitHub's own spelling, in schema order.
    assert plan.delete == ("design needed", "platform:ios", "Apple")
    ops = plan.ops()
    assert isinstance(ops[0], CreateLabel)
    assert [type(op) for op in ops[1:4]] == [Relabel, Relabel, Relabel]
    assert ops[4:] == [
        DeleteLabel("design needed"),
        DeleteLabel("platform:ios"),
        DeleteLabel("Apple"),
    ]


def test_migration_without_aliases_on_github_is_empty() -> None:
    plan = plan_migration(REPO, SCHEMA, ["bug", "area:apple"], [issue(1, "wiki")])
    assert plan == MigrationPlan((), {}, ())
    assert plan.ops() == []


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
