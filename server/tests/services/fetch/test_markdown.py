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


def test_should_dedent_only_a_lone_link_line_after_a_heading() -> None:
    angle = "[a b](<https://example.org/a b>)"
    assert normalize_markdown(f"## H\n    {angle}\n") == f"## H\n{angle}\n"
    nested = "[see [this] \\] one](https://example.org/a_(b))"
    assert normalize_markdown(f"## H\n    {nested}") == f"## H\n{nested}"
    untouched = [
        "### Usage\n    make install\n    make test\n",  # real indented code
        f"### Usage\n    {LINK}\n    make test",  # a link opening a longer code block
        "### Usage\n    plain prose, not a link",
        "### Usage\n    [a](https://example.org/x) and more",
        "### Usage\n    [a](https://example.org/x)(y)",
        "### Usage\n    [a](https://example.org/x y)",
        "### Usage\n    [a](<https://example.org/x> y)",
        "### Usage\n    [a]https://example.org/x",
        "### Usage\n    [unclosed",
        "### Usage\n    \\[a](https://example.org/x)",
    ]
    for markdown in untouched:
        assert normalize_markdown(markdown) == markdown, markdown


def test_should_leave_an_indented_line_after_a_closing_fence() -> None:
    markdown = "```\ndef f():\n    return 1\n```\n        [a](https://example.org/x)\n"
    assert normalize_markdown(markdown) == markdown


def test_should_drop_an_empty_heading_before_a_block_it_cannot_join() -> None:
    blocks = [
        "- first\n- second",
        "* first",
        "+ first",
        "-",
        "1. first",
        "12) first",
        "> a quote",
        "    indented code\n    more code",
        "| a | b |",
        "<div>html</div>",
        "---",
        "* * *",
        "___",
        "===",
        "~~~\ncode\n~~~",
    ]
    for block in blocks:
        assert normalize_markdown(f"Intro.\n\n###\n{block}") == f"Intro.\n\n{block}", block


def test_should_join_an_empty_heading_with_text_that_only_looks_like_a_block() -> None:
    for text in ("-dash prose", "1.5 litres", "2026 was a year", "*emphasis* first", "--x"):
        assert normalize_markdown(f"###\n{text}") == f"### {text}", text


def test_should_respect_commonmark_fence_indentation() -> None:
    # A line indented four spaces is code, not a fence: it neither opens nor closes one.
    inside = "```\n    ```\n### \nnot a heading\n```\n"
    assert normalize_markdown(inside) == inside
    assert normalize_markdown("   ```\n### \n   ```\n") == "   ```\n### \n   ```\n"
    assert normalize_markdown("    ```\n### \nreal heading") == "    ```\n### real heading"
    # A closer has nothing after it; a backtick opener's info string has no backtick.
    trailing = "```\n``` not a closer\n### \nstill code\n```"
    assert normalize_markdown(trailing) == trailing
    assert normalize_markdown("``` a`b\n### \ntext") == "``` a`b\n### text"


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
