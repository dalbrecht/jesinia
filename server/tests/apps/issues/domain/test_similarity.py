# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local TF-IDF that pre-filters duplicate candidates (plan Task 5)."""

from __future__ import annotations

import pytest

from jesinia.apps.issues.domain.similarity import (
    BODY_CHARS,
    Match,
    TfIdf,
    document_text,
    tokens,
)


def test_tokens_are_lowercase_words_without_stopwords_or_single_letters() -> None:
    assert tokens("The Login button CRASHES on iOS 17, a b") == [
        "login",
        "button",
        "crashes",
        "ios",
        "17",
    ]
    assert tokens("") == []
    assert tokens("Überprüfung fehlgeschlagen") == ["überprüfung", "fehlgeschlagen"]


def test_document_text_weights_the_title_and_caps_the_body() -> None:
    text = document_text("Crash", "x" * (BODY_CHARS + 50))
    assert text.startswith("Crash\nCrash\n")
    assert len(text) == len("Crash\nCrash\n") + BODY_CHARS


def test_similar_ranks_by_cosine_and_excludes_the_document_itself() -> None:
    index = TfIdf(
        {
            ("acme/web", 1): "login button crashes on ios",
            ("acme/web", 2): "ios login crashes after update",
            ("acme/web", 3): "dark mode colours are off",
            ("acme/api", 4): "login crashes",
        }
    )
    found = index.similar(("acme/web", 1), limit=5, minimum=0.1)
    keys = [match.key for match in found]
    assert ("acme/web", 1) not in keys
    assert ("acme/web", 3) not in keys  # nothing in common
    assert set(keys) == {("acme/web", 2), ("acme/api", 4)}
    assert all(isinstance(m, Match) and 0.1 <= m.score <= 1.0 for m in found)
    assert found == sorted(found, key=lambda m: (-m.score, m.key))


def test_similar_respects_limit_and_minimum_and_unknown_or_empty_documents() -> None:
    index = TfIdf(
        {("r/a", 1): "alpha beta", ("r/a", 2): "alpha beta", ("r/a", 3): "alpha", ("r/a", 4): ""}
    )
    assert [m.key for m in index.similar(("r/a", 1), limit=1, minimum=0.0)] == [("r/a", 2)]
    [best] = index.similar(("r/a", 1), limit=5, minimum=0.999)
    assert best.key == ("r/a", 2) and best.score == pytest.approx(1.0)
    assert index.similar(("r/a", 4), limit=5, minimum=0.0) == []  # no words, nothing similar
    assert index.similar(("r/a", 9), limit=5, minimum=0.0) == []  # not in the corpus
    assert index.similar(("r/a", 1), limit=0, minimum=0.0) == []
