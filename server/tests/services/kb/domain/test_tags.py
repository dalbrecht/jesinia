# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from jesinia.services.kb.domain.tags import (
    TAG_LINE_PREFIX,
    extract_tags,
    is_valid_tag,
    near_matches,
    normalise_tag,
    render_tag_line,
    replace_tag,
)


def test_extract_ignores_code_urls_headings_and_suffix_hashes() -> None:
    body = (
        "# Heading #nope\nUses #SQLite and #wal-mode.\n`#include` x\n```\n#code\n```\n"
        "C# and https://x/#frag #2026\n"
    )
    assert extract_tags(body) == ["sqlite", "wal-mode", "2026"]


def test_extract_dedupes_case_insensitively_and_handles_tilde_fences() -> None:
    body = "#Rust #rust\n~~~\n#hidden\n~~~\n#after\n"
    assert extract_tags(body) == ["rust", "after"]


def test_extract_handles_unicode_and_unclosed_fence() -> None:
    assert extract_tags("#café ok\n```\n#gone\n") == ["café"]


def test_normalise() -> None:
    assert normalise_tag("#Machine Learning") == "machine-learning"
    assert normalise_tag("sql_lite") == "sql-lite"


def test_replace_tag_rewrites_only_real_tags() -> None:
    body = "See #sqlite, #SQLite3 and `#sqlite`.\n"
    assert replace_tag(body, "sqlite", "databases") == "See #databases, #SQLite3 and `#sqlite`.\n"


def test_replace_tag_is_case_insensitive_and_writes_lowercase() -> None:
    assert replace_tag("#SQLite x", "#sqlite", "Data Base") == "#data-base x"


def test_near_matches() -> None:
    known = ["sqlite", "databases", "machine-learning", "ml"]
    assert near_matches("SQLite", known) == ["sqlite"]
    assert near_matches("database", known) == ["databases"]
    assert near_matches("sql-lite", known) == ["sqlite"]
    assert near_matches("machine_learnin", known) == ["machine-learning"]
    assert near_matches("ml", known) == ["ml"]
    assert near_matches("rust", known) == []


def test_near_matches_requires_five_letters_for_edit_distance() -> None:
    assert near_matches("cats", ["cuts"]) == []
    assert near_matches("python", ["pyton", "pyth"]) == ["pyton"]


def test_render_tag_line() -> None:
    assert TAG_LINE_PREFIX == "Tags:"
    assert render_tag_line(["a", "b"]) == "Tags: #a #b"


def test_is_valid_tag_accepts_only_normalised_grammar_tags() -> None:
    assert is_valid_tag("sqlite") and is_valid_tag("write-ahead-log") and is_valid_tag("café")
    assert is_valid_tag("3d")
    for bad in ("", "-x", "_x", "a/b", "a_b", "A", "a b", "#a"):
        assert not is_valid_tag(bad), bad
