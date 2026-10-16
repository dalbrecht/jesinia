# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import PurePosixPath

import pytest

from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.paths import (
    filename_for,
    is_hidden,
    is_page_candidate,
    is_reserved,
    sanitize_title,
)

A = PageId("0192f7a8-0000-7000-8000-00000000000a")
B = PageId("0192f7a8-0000-7000-8000-00000000000b")


def test_sanitize_and_nfc() -> None:
    assert sanitize_title("a/b: c.. ") == "a b c"
    assert sanitize_title("Café") == "Café"
    assert sanitize_title("  ") == "Untitled"
    assert sanitize_title("tab\there\x00x") == "tab here x"
    assert sanitize_title("back\\slash") == "back slash"
    assert sanitize_title("...") == "Untitled"


def test_collision_suffix_is_bracketed_id_prefix() -> None:
    assert filename_for("Note", A, [("note", B)]) == "Note.md"
    assert filename_for("Note", B, [("Note", A)]) == "Note [0192f7a8].md"
    assert filename_for("Note", A, [("Other", B)]) == "Note.md"
    assert filename_for("Note", A, [("Note", A)]) == "Note.md"  # itself is not a clash
    assert filename_for("a/b", A, []) == "a b.md"


def test_reserved_and_hidden() -> None:
    assert is_reserved(PurePosixPath("journal/x.md")) and is_reserved(PurePosixPath("Drafts/x.md"))
    assert not is_reserved(PurePosixPath("Notes/Journal/x.md"))
    assert is_hidden(PurePosixPath("a/.obsidian/x.md")) and not is_hidden(PurePosixPath("a/b.md"))


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("Notes/a.md", True),
        ("Notes/a.MD", True),
        ("Notes/a.txt", False),
        (".hidden/a.md", False),
        ("_assets/123/a.md", False),
        ("Notes/_assets/a.md", False),
    ],
)
def test_is_page_candidate(path: str, ok: bool) -> None:
    assert is_page_candidate(PurePosixPath(path)) is ok
