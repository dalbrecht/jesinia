# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from jesinia.services.kbsync.domain.message import (
    StatusEntry,
    commit_message,
    parse_name_status,
    summarize_changes,
)
from jesinia.services.kbsync.domain.state import PROBLEM_TEXT, GitState


def test_summary() -> None:
    e = [
        StatusEntry("M", "Sources/a.md"),
        StatusEntry("M", "Sources/b.md"),
        StatusEntry("M", "Notes/c.md"),
        StatusEntry("A", "Sources/d.md"),
    ]
    assert summarize_changes(e) == "kb: 1 added, 3 updated (Sources/…, Notes/…)"


def test_summary_all_kinds_root_files_and_scope_cap() -> None:
    e = [
        StatusEntry("D", "a/x.md"),
        StatusEntry("R", "b/y.md"),
        StatusEntry("A", "c/z.md"),
        StatusEntry("M", "d/w.md"),
        StatusEntry("M", "README.md"),
    ]
    assert summarize_changes(e) == "kb: 1 added, 2 updated, 1 deleted, 1 renamed (a/…, b/…, c/…, …)"
    assert summarize_changes([StatusEntry("M", "Inbox.md")]) == "kb: 1 updated (Inbox.md)"
    assert summarize_changes([]) == "kb: no changes"


def test_commit_message_has_trailer() -> None:
    msg = commit_message([StatusEntry("A", "Notes/n.md")])
    assert msg == "kb: 1 added (Notes/…)\n\nJesinia-Auto-Sync: true\n"


def test_parse_name_status() -> None:
    raw = "M\tNotes/a.md\nA\tNotes/b c.md\nD\told.md\nR087\tfrom.md\tto/new.md\n"
    assert parse_name_status(raw) == [
        StatusEntry("M", "Notes/a.md"),
        StatusEntry("A", "Notes/b c.md"),
        StatusEntry("D", "old.md"),
        StatusEntry("R", "to/new.md"),
    ]
    assert parse_name_status("") == []
    assert parse_name_status("garbage without a tab\n") == []
    assert parse_name_status("T\tlink.md\nC100\ta.md\tb.md\n") == [
        StatusEntry("M", "link.md"),
        StatusEntry("A", "b.md"),
    ]


@pytest.mark.parametrize(
    "problem", ["detached-head", "rebase-in-progress", "merge-in-progress", "no-upstream"]
)
def test_problem_text(problem: str) -> None:
    st = GitState("commit", None, None, 0, 0, False, problem)  # type: ignore[arg-type]
    assert st.problem_text == PROBLEM_TEXT[problem]  # type: ignore[index]
    assert GitState("off", None, None, 0, 0, False, None).problem_text is None
    assert not GitState("push", "main", "origin/main", 0, 0, False, None).blocked
    assert GitState("push", None, None, 0, 0, False, "detached-head").blocked
