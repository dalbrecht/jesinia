# SPDX-License-Identifier: AGPL-3.0-or-later
from markdown_it import MarkdownIt

from jesinia.services.fetch.domain.markdown import normalize_markdown

LINK = "[step 1: cut it open](https://jvns.ca/post/#step-1)"


def _token_types(markdown: str) -> list[str]:
    return [token.type for token in MarkdownIt("commonmark").parse(markdown)]


def test_should_merge_an_empty_heading_with_the_indented_link_after_it() -> None:
    broken = f"Intro.\n\n### \n      {LINK}\n    \n\nBody."
    assert normalize_markdown(broken) == f"Intro.\n\n### {LINK}\n    \n\nBody."


def test_should_merge_an_empty_heading_with_plain_text_on_the_next_line() -> None:
    assert normalize_markdown("##\nwhat I learned\n\nBody.") == "## what I learned\n\nBody."


def test_should_drop_an_empty_heading_with_nothing_to_merge() -> None:
    assert normalize_markdown("Intro.\n\n### \n\nBody.") == "Intro.\n\n\nBody."
    assert normalize_markdown("Intro.\n\n## #  ") == "Intro.\n"
    assert normalize_markdown("### \n## Next") == "## Next"
    assert normalize_markdown("### \n```\ncode\n```") == "```\ncode\n```"


def test_should_dedent_an_indented_line_right_after_a_heading() -> None:
    fixed = normalize_markdown(f"### Steps\n      {LINK}\n\nBody.")
    assert fixed == f"### Steps\n{LINK}\n\nBody."
    assert "code_block" not in _token_types(fixed)


def test_should_dedent_prose_right_after_a_closing_fence() -> None:
    broken = "```\ndef f():\n    return 1\n```\n        Next time I will know.\n"
    fixed = normalize_markdown(broken)
    assert fixed == "```\ndef f():\n    return 1\n```\nNext time I will know.\n"
    assert _token_types(fixed) == ["fence", "paragraph_open", "inline", "paragraph_close"]


def test_should_leave_fenced_code_untouched() -> None:
    fenced = "~~~~\n### \n    indented in code\n~~~\n#\n~~~~\nText.\n````\n```\n## \n````"
    assert normalize_markdown(fenced) == fenced


def test_should_leave_indented_code_lists_and_continuations_untouched() -> None:
    markdown = (
        "Intro paragraph with a line\n      that continues indented.\n\n"
        "    real indented code\n\n"
        "- item\n    - nested item\n\n"
        "  ```\n  listed code\n  ```\n      list continuation\n\n"
        "#hashtag is not a heading\n"
        "####### seven hashes is not a heading\n    still a paragraph"
    )
    assert normalize_markdown(markdown) == markdown


def test_should_keep_a_heading_followed_by_a_blank_line() -> None:
    markdown = "## Heading\n\n    indented code after a blank line"
    assert normalize_markdown(markdown) == markdown
