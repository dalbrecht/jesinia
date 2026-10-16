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


def test_extract_ignores_link_anchors_query_strings_and_indented_code() -> None:
    body = (
        "[jump](#anchor) a?q=#query x=#eq &#amp ok #real\n"
        "\n    #indented-code\n    #more-code\n\t#tab-code\nback to #prose\n"
        "- item #listed\n    - nested #nested\n"
        "\n\t- tab list #tab-listed\n"
    )
    assert extract_tags(body) == ["real", "prose", "listed", "nested", "tab-listed"]


def test_indented_lines_without_a_blank_line_before_are_not_code() -> None:
    assert extract_tags("para\n    #continued\n") == ["continued"]
    assert extract_tags("    #first-line-code\n") == []


def test_replace_tag_leaves_link_anchors_alone() -> None:
    body = "[x](#sqlite) #sqlite\n"
    assert replace_tag(body, "sqlite", "db") == "[x](#sqlite) #db\n"


def test_parenthesised_tag_in_prose_is_a_tag() -> None:
    assert extract_tags("Uses SQLite (#sqlite) daily.\n") == ["sqlite"]
    assert replace_tag("(#sqlite)", "sqlite", "db") == "(#db)"


def test_markdown_link_targets_are_still_not_tags() -> None:
    assert extract_tags("[x](#a) and [y](#b-c)\n") == []


def test_underscore_joined_words_are_not_tags_or_prefixes_of_tags() -> None:
    assert extract_tags("#machine_learning and #b_c and #machine\n") == ["machine"]
    assert extract_tags("#foo-bar_baz\n") == []


def test_rename_does_not_touch_underscore_joined_words() -> None:
    body = "#machine_learning and #machine"
    assert replace_tag(body, "machine", "ml") == "#machine_learning and #ml"


def test_decomposed_combining_marks_are_part_of_the_tag() -> None:
    decomposed = "#café and #cafe"
    assert extract_tags(decomposed) == ["café", "cafe"]
    assert extract_tags("é#tag") == []


def test_rename_keeps_decomposed_tags_and_the_rest_of_the_text_unchanged() -> None:
    decomposed = "Café #café and #cafe."
    assert replace_tag(decomposed, "cafe", "coffee") == "Café #café and #coffee."
    assert replace_tag(decomposed, "café", "coffee") == "Café #coffee and #cafe."


def test_trailing_hyphen_is_not_part_of_the_tag() -> None:
    assert extract_tags("#foo- and #bar--baz\n") == ["foo", "bar"]
    assert replace_tag("#foo- x", "foo", "qux") == "#qux- x"


def test_is_valid_tag_rejects_doubled_and_trailing_hyphens() -> None:
    assert not is_valid_tag("a-") and not is_valid_tag("a--b")


def test_tag_identity_uses_unicode_case_folding() -> None:
    assert normalise_tag("#STRASSE") == normalise_tag("#straße") == "strasse"
    assert extract_tags("#straße and #STRASSE and #Ünï\n") == ["strasse", "ünï"]


def test_replace_tag_renames_every_case_fold_spelling() -> None:
    assert replace_tag("#straße x #STRASSE\n", "strasse", "road") == "#road x #road\n"
    assert replace_tag("#Straße\n", "#STRASSE", "road") == "#road\n"


def test_longer_fence_is_not_closed_by_shorter_or_other_character_lines() -> None:
    body = "````\n#a\n```\n#b\n~~~~\n#c\n````\n#after\n"
    assert extract_tags(body) == ["after"]


def test_fence_closes_on_equal_or_longer_run_of_the_same_character() -> None:
    assert extract_tags("```\n#a\n`````\n#after\n") == ["after"]
    assert extract_tags("~~~~\n#a\n~~~~~\n#after\n") == ["after"]
    assert extract_tags("```\n#a\n```trailing\n#b\n```\n#after\n") == ["after"]


def test_inline_code_span_crossing_a_newline_is_masked() -> None:
    body = "see `code\n#hidden` then #shown\n"
    assert extract_tags(body) == ["shown"]
    assert replace_tag(body, "hidden", "x") == body


def test_inline_code_span_is_broken_by_a_blank_line() -> None:
    body = "an `unclosed\n\n#visible` tail\n"
    assert extract_tags(body) == ["visible"]


def test_inline_code_span_needs_a_matching_backtick_run_across_lines() -> None:
    body = "``a\n#hidden` still``\n#after `x\n#kept\n"
    assert extract_tags(body) == ["after", "kept"]
