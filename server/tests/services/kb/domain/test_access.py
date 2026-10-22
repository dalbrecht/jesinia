# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import PurePosixPath

import pytest

from jesinia.services.kb.domain.access import AgentOnly, lock_scope

P = PurePosixPath


@pytest.mark.parametrize(
    ("jesinia", "scope"),
    [
        ({}, None),
        ({"kind": "folder"}, None),
        ({"access": "agents"}, "page"),
        ({"access": "agents", "kind": "note"}, "page"),
        ({"access": "agents", "kind": "folder"}, "folder"),
        ({"access": "Agents"}, None),  # exact value only: a typo never half-locks
        ({"access": ["agents"]}, None),
        ({"access": "everyone", "kind": "folder"}, None),
    ],
)
def test_should_read_the_lock_scope_from_jesinia_metadata(
    jesinia: dict[str, object], scope: str | None
) -> None:
    assert lock_scope(jesinia) == scope


def test_should_lock_a_whole_folder_and_its_subfolders() -> None:
    rules = AgentOnly.of([(P("91 Memory/Memory.md"), "folder")])
    assert rules.covers(P("91 Memory/Memory.md"))
    assert rules.covers(P("91 Memory/claude/Prefs.md"))
    assert not rules.covers(P("90 Maps/KB Home.md"))
    assert not rules.covers(P("91 Memory Old/x.md"))  # a sibling with the same prefix
    assert rules.covers_folder(P("91 Memory"))
    assert rules.covers_folder(P("91 Memory/deep/er"))
    assert not rules.covers_folder(P("00 Inbox"))
    assert not rules.covers_folder(P(""))


def test_should_lock_only_the_page_itself_for_page_scope() -> None:
    rules = AgentOnly.of([(P("Notes/Secret.md"), "page")])
    assert rules.covers(P("Notes/Secret.md"))
    assert not rules.covers(P("Notes/Open.md"))
    assert not rules.covers_folder(P("Notes"))


def test_should_match_paths_case_insensitively_like_the_mac_file_system() -> None:
    rules = AgentOnly.of([(P("93 Scratch/Scratch.md"), "folder"), (P("A/B.md"), "page")])
    assert rules.covers(P("93 scratch/x.md"))
    assert rules.covers_folder(P("93 SCRATCH"))
    assert rules.covers(P("a/b.md"))


def test_should_never_lock_the_whole_kb_from_a_root_folder_page() -> None:
    rules = AgentOnly.of([(P("Home.md"), "folder")])
    assert rules.covers(P("Home.md"))  # still locks itself
    assert not rules.covers(P("Other.md"))
    assert not rules.covers_folder(P(""))


def test_empty_rules_lock_nothing() -> None:
    rules = AgentOnly.of([])
    assert not rules and not rules.covers(P("x.md")) and not rules.covers_folder(P("x"))
    assert AgentOnly.of([(P("a.md"), "page")])
