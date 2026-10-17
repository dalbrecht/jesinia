# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small local TF-IDF over issue titles and bodies (plan Task 5): it pre-filters the
duplicate candidates an agent may pick from, so a suggested duplicate is always an issue whose
text really overlaps. Pure, no I/O, no third-party code: a few thousand cached issues index in
milliseconds.

Weights are smoothed ``idf = ln((1 + N) / (1 + df)) + 1`` times raw term counts, compared by
cosine similarity, so a score is in ``[0, 1]``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

DocKey = tuple[str, int]
# Characters of an issue body indexed: long bodies (logs, templates) only add noise.
BODY_CHARS = 4000
_WORD = re.compile(r"[^\W_]+")
# Common English words that carry no topic; letters alone are dropped too.
_STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from has have how i if in into is it its "
    "not of on or so that the then there this to was we were what when where which while who "
    "will with would you".split()
)


def tokens(text: str) -> list[str]:
    """``text``'s words, lowercased, without stopwords or single characters."""
    return [
        word for word in _WORD.findall(text.casefold()) if len(word) > 1 and word not in _STOPWORDS
    ]


def document_text(title: str, body: str) -> str:
    """What an issue is indexed as: its title twice (titles name the problem) and the start of
    its body."""
    return f"{title}\n{title}\n{body[:BODY_CHARS]}"


@dataclass(frozen=True)
class Match:
    key: DocKey
    score: float


class TfIdf:
    """Cosine similarity between the documents of one corpus.

    Args:
        documents: each document's text by its key (``(repo, number)``).
    """

    def __init__(self, documents: Mapping[DocKey, str]) -> None:
        counts = {key: Counter(tokens(text)) for key, text in documents.items()}
        frequency: Counter[str] = Counter()
        for terms in counts.values():
            frequency.update(terms.keys())
        total = len(counts)
        idf = {term: math.log((1 + total) / (1 + df)) + 1 for term, df in frequency.items()}
        self._vectors = {key: _unit(terms, idf) for key, terms in counts.items()}

    def similar(self, key: DocKey, *, limit: int, minimum: float) -> list[Match]:
        """The documents most like ``key``'s, best first (ties by key), each scoring at least
        ``minimum``; never ``key`` itself. A key outside the corpus, or a document without
        words, has no matches."""
        vector = self._vectors.get(key)
        if not vector or limit <= 0:
            return []
        found = [
            Match(other, score)
            for other, candidate in self._vectors.items()
            if other != key and (score := _dot(vector, candidate)) >= minimum and score > 0
        ]
        found.sort(key=lambda match: (-match.score, match.key))
        return found[:limit]


def _unit(terms: Counter[str], idf: Mapping[str, float]) -> dict[str, float]:
    weights = {term: count * idf[term] for term, count in terms.items()}
    norm = math.sqrt(sum(weight * weight for weight in weights.values()))
    return {term: weight / norm for term, weight in weights.items()} if norm else {}


def _dot(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    small, large = (left, right) if len(left) <= len(right) else (right, left)
    return min(1.0, sum(weight * large.get(term, 0.0) for term, weight in small.items()))
