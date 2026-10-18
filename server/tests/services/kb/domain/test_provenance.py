# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import UTC, datetime, timedelta, timezone

from jesinia.services.kb.domain.links import link_targets, retitle_links
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.provenance import inert, provenance_label, wrap_ai


def test_wrap_ai_exact() -> None:
    out = wrap_ai(
        "Hi",
        agent="claude-code",
        model="claude-opus-5-5",
        at=datetime(2026, 10, 16, 9, 20, tzinfo=UTC),
        session="s1",
    )
    assert out == (
        "<!-- jesinia:ai agent=claude-code model=claude-opus-5-5 at=2026-10-16T09:20:00Z "
        "session=s1 -->\nHi\n<!-- /jesinia:ai -->"
    )


def test_wrap_ai_converts_to_utc() -> None:
    pdt = timezone(timedelta(hours=-7))
    out = wrap_ai(
        "x", agent="a", model="m", at=datetime(2026, 10, 16, 2, 20, tzinfo=pdt), session="s"
    )
    assert "at=2026-10-16T09:20:00Z" in out


def test_inert_neutralises_comment_markers() -> None:
    assert inert("a <!-- b --> c") == "a &lt;!-- b --&gt; c"


def test_provenance_label_is_one_safe_token() -> None:
    assert provenance_label("claude-opus-5-5") == "claude-opus-5-5"
    # Issue #15: a bracketed context suffix isn't part of the model's name.
    assert provenance_label("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert provenance_label("[1m]") == "unknown"
    assert provenance_label("gpt 5 -->") == "gpt-5----"
    assert provenance_label("") == "unknown"
    assert len(provenance_label("x" * 200)) == 80


def test_retitle_links_only_touches_that_id() -> None:
    a, b = "0192f7a8-0000-7000-8000-00000000000a", "0192f7a8-0000-7000-8000-00000000000b"
    body = f"See [[{a}|Old]] and [[{b}|Other]] and [[{a}]]."
    assert (
        retitle_links(body, PageId(a), "New") == f"See [[{a}|New]] and [[{b}|Other]] and [[{a}]]."
    )


def test_retitle_links_sanitizes_link_breaking_chars() -> None:
    a = "0192f7a8-0000-7000-8000-00000000000a"
    assert retitle_links(f"[[{a}|x]]", PageId(a), "a|b]]c") == f"[[{a}|a b c]]"


def test_link_targets() -> None:
    a, b = "0192f7a8-0000-7000-8000-00000000000a", "0192f7a8-0000-7000-8000-00000000000b"
    assert link_targets(f"[[{a}|A]] [[{b}]] [[not-an-id]] [[{a}]]") == {PageId(a), PageId(b)}
