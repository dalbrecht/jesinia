# SPDX-License-Identifier: AGPL-3.0-or-later
"""Theme rules (spec §5.1, §6.2; plan Task 10; rulings IR4, IR15, IR17, IR27, IR49): a theme's
tag, its ``theme:`` label and the plan turning it into an epic. Pure."""

from __future__ import annotations

import pytest

from jesinia.apps.issues.domain.changes import (
    CreateIssue,
    EnsureLabel,
    Existing,
    New,
    SetParent,
)
from jesinia.apps.issues.domain.epics import CROSS_REPO_WARNING, Facts, Node
from jesinia.apps.issues.domain.themes import (
    THEME_PREFIX,
    InvalidThemeError,
    ThemeDomainError,
    has_theme_label,
    is_publishable,
    theme_epic_plan,
    theme_label,
    theme_label_change,
    theme_tag,
)
from jesinia.services.github.domain.issue import IssueRef

REPO, OTHER, ELSEWHERE = "acme/web", "acme/api", "globex/web"


def facts(n: int, repo: str = REPO, parent: tuple[str, int] | None = None) -> Facts:
    return Facts(repo, n, True, parent, False)


def test_a_theme_is_a_normalised_tag() -> None:
    assert theme_tag("#Sync Reliability") == "sync-reliability"
    assert theme_tag("café") == "café"
    for bad in ("", "#", "no good!", "a_b?"):
        with pytest.raises(InvalidThemeError):
            theme_tag(bad)
    assert issubclass(InvalidThemeError, ThemeDomainError)


def test_a_theme_label_is_the_prefix_and_the_tag() -> None:
    assert THEME_PREFIX == "theme:"
    assert theme_label("sync") == "theme:sync"
    with pytest.raises(InvalidThemeError, match="label"):
        theme_label("x" * 60)


def test_the_label_setup_step_creates_the_theme_label_with_no_schema_entry() -> None:
    change = theme_label_change(REPO, "sync")
    assert isinstance(change, EnsureLabel)
    assert (change.repo, change.name, change.schema_entry) == (REPO, "theme:sync", None)
    assert "#sync" in change.description


def test_has_theme_label_ignores_case_and_spacing() -> None:
    assert has_theme_label(("bug", " Theme:Sync "), "sync")
    assert not has_theme_label(("theme:sync-later",), "sync")


def test_theme_epic_plan_creates_the_epic_and_links_the_tags_issues() -> None:
    members = [
        facts(1),
        facts(2, OTHER),
        facts(3, ELSEWHERE),
        facts(4, parent=(REPO, 9)),
    ]
    plan = theme_epic_plan("sync`@x", REPO, members, uncached=[(REPO, 7)])
    assert isinstance(plan.epic, CreateIssue) and plan.epic.repo == REPO
    assert plan.epic.labels == ()  # its children make it an epic (IR4)
    # Jesinia's own text carries the tag only as code (IR49).
    assert "``sync`@x``" in plan.epic.title and "``#sync`@x``" in plan.epic.body
    assert [(c.target, c.warning) for c in plan.children] == [
        (Existing(REPO, 1), None),
        (Existing(OTHER, 2), CROSS_REPO_WARNING),
    ]
    reasons = {s.item: s.reason for s in plan.skipped}
    assert "same owner" in reasons[f"{ELSEWHERE}#3"]
    assert reasons[f"{REPO}#4"] == f"already under {REPO}#9"
    assert "not in the cache" in reasons[f"{REPO}#7"]
    issues = dict(plan.changes.issues)
    assert issues[Existing(REPO, 1)][0] == SetParent(New("epic"), True, None)
    assert plan.digest == theme_epic_plan("sync`@x", REPO, members, [(REPO, 7)]).digest


def test_theme_epic_plan_goes_in_one_of_the_tags_repos() -> None:
    with pytest.raises(InvalidThemeError, match="acme/web"):
        theme_epic_plan("sync", "acme/other", [facts(1)], uncached=[])
    # An uncached issue's repo counts too.
    plan = theme_epic_plan("sync", OTHER, [facts(1)], uncached=[(OTHER, 4)])
    assert isinstance(plan.epic, CreateIssue) and plan.epic.repo == OTHER


def test_theme_epic_plan_reuses_an_epic_an_unfinished_batch_created() -> None:
    epic = Node(REPO, 50)
    members = [facts(1, parent=(REPO, 50)), facts(2), facts(3, parent=(REPO, 9))]
    plan = theme_epic_plan("sync", REPO, members, uncached=[], existing=epic, ancestors=())
    assert plan.epic == IssueRef(REPO, 50) and plan.changes.repo == ()
    assert [c.target for c in plan.children] == [Existing(REPO, 2)]
    assert dict(plan.changes.issues)[Existing(REPO, 2)][0] == SetParent(
        Existing(REPO, 50), True, None
    )
    assert {s.item for s in plan.skipped} == {f"{REPO}#1", f"{REPO}#3"}


def test_a_theme_is_publishable_only_when_its_label_fits() -> None:
    assert is_publishable("sync")
    assert not is_publishable("a" * 45)
