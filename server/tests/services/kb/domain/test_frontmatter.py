# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import PurePosixPath

import pytest

from jesinia.services.kb.domain.frontmatter import (
    page_from,
    parse_document,
    replace_body,
    serialize_page,
    split_document,
    title_scalar,
)
from jesinia.services.kb.domain.page import Page, PageId, new_page_id

ID = "0192f7a8-0000-7000-8000-000000000001"


def test_round_trip_preserves_foreign_keys_and_order() -> None:
    text = f"---\nid: {ID}\ntitle: Round Trip\naliases: [rt]\njesinia:\n  kind: note\n---\nBody\n"
    doc = parse_document(text)
    page = page_from(doc, PurePosixPath("Notes/Round Trip.md"))
    out = serialize_page(page)
    assert out.splitlines()[1:4] == [f"id: {ID}", "title: Round Trip", "aliases:"]
    reparsed = parse_document(out)
    assert reparsed.frontmatter is not None and reparsed.frontmatter["aliases"] == ["rt"]
    assert reparsed.frontmatter["jesinia"] == {"kind": "note"}
    assert out.splitlines()[-2:] == ["---", "Body"]


def test_jesinia_is_last_and_foreign_order_kept() -> None:
    text = f"---\nzeta: 1\nid: {ID}\njesinia: {{kind: note}}\ntitle: T\nalpha: 2\n---\nb"
    out = serialize_page(page_from(parse_document(text), PurePosixPath("T.md")))
    keys = [
        ln.split(":")[0]
        for ln in out.split("---")[1].strip().splitlines()
        if not ln.startswith(" ")
    ]
    assert keys == ["id", "title", "zeta", "alpha", "jesinia"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Plain", "Plain"),
        ("yes", '"yes"'),
        ("a: b", '"a: b"'),
        ("123", '"123"'),
        ("#x", '"#x"'),
        ("", '""'),
        ("line\nbreak", '"line\\nbreak"'),
        ("[x", '"[x"'),
        ("Café", "Café"),
    ],
)
def test_title_scalar(title: str, expected: str) -> None:
    assert title_scalar(title) == expected


def test_classify() -> None:
    assert parse_document("no fence\n").kind == "plain"
    assert parse_document("---\nlayout: post\n---\nx").kind == "foreign"
    assert parse_document("---\n: : bad\n---\nx").kind == "foreign"
    assert parse_document("---\n- a list\n---\nx").kind == "foreign"
    assert parse_document("---\nid: 1\nnot closed\n").kind == "foreign"
    page = parse_document(f"---\nid: {ID}\n---\nx")
    assert page.kind == "page" and page.body == "x"


def test_crlf_and_dot_terminator() -> None:
    doc = parse_document(f"---\r\nid: {ID}\r\ntitle: W\r\n...\r\nbody\r\n")
    assert doc.kind == "page" and doc.body == "body\r\n"


def test_page_from_defaults_title_to_filename() -> None:
    page = page_from(parse_document(f"---\nid: {ID}\n---\nx"), PurePosixPath("Inbox/My Idea.md"))
    assert page.title == "My Idea" and page.jesinia == {} and page.foreign == {}


def test_page_from_requires_page_kind() -> None:
    with pytest.raises(ValueError):
        page_from(parse_document("plain"), PurePosixPath("x.md"))


def test_page_from_rejects_non_mapping_jesinia() -> None:
    with pytest.raises(ValueError):
        page_from(parse_document(f"---\nid: {ID}\njesinia: [1]\n---\n"), PurePosixPath("x.md"))


def test_replace_body_keeps_raw_frontmatter_bytes() -> None:
    text = (
        f"---\nid: {ID}\ntitle: 'Quoted'   # note\naliases: [rt, \"x\"]\n"
        "jesinia: {kind: note}\n---\nOld body\n"
    )
    raw, body = split_document(text)
    assert raw is not None and body == "Old body\n"
    out = replace_body(text, "New body\n")
    assert out == raw + "New body\n" and out.startswith(text[: len(raw)])


def test_split_and_replace_without_frontmatter() -> None:
    assert split_document("just text") == (None, "just text")
    assert replace_body("just text", "new") == "new"
    assert split_document("---\nunclosed") == (None, "---\nunclosed")


def test_serialize_minimal_page() -> None:
    page = Page(PageId(ID), "Hello", PurePosixPath("Notes/Hello.md"), "Body", {}, {})
    assert serialize_page(page) == f"---\nid: {ID}\ntitle: Hello\n---\nBody"


def test_new_page_id_is_uuid7() -> None:
    a, b = new_page_id(), new_page_id()
    assert a != b and len(a) == 36 and a[14] == "7" and a[19] in "89ab" and a == a.lower()
