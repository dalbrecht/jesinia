# SPDX-License-Identifier: AGPL-3.0-or-later
"""Metrics over time (spec §5.2): a round's scores as one point of its repo's trend.

Pure: no I/O. A round's ``metrics`` artifact is agent output that passed the job's schema
when it was stored, but it is read back as untrusted JSON all the same: an entry that isn't a
named, finite score from 0 to 10 is left out, never guessed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

SCORE_MIN = 0.0
SCORE_MAX = 10.0


@dataclass(frozen=True)
class Score:
    """One criterion's score in a round (agent text: render the criterion as text)."""

    criterion: str
    score: float


@dataclass(frozen=True)
class Point:
    """One scored round of a repo: its PR, the head it reviewed, when, and its scores."""

    round_id: int
    number: int
    head_sha: str
    at: datetime
    scores: tuple[Score, ...]

    @property
    def mean(self) -> float:
        """The round's overall score: the mean over its criteria (a point has at least one)."""
        return sum(s.score for s in self.scores) / len(self.scores)


def _score(item: object) -> Score | None:
    if not isinstance(item, Mapping):
        return None
    criterion, score = item.get("criterion"), item.get("score")
    if not isinstance(criterion, str) or not criterion:
        return None
    # ``bool`` is an ``int``: a ``true`` score is no score.
    if isinstance(score, bool) or not isinstance(score, int | float):
        return None
    value = float(score)
    if not math.isfinite(value) or not SCORE_MIN <= value <= SCORE_MAX:
        return None
    return Score(criterion, value)


def point_of(
    round_id: int, number: int, head_sha: str, at: datetime, metrics: object
) -> Point | None:
    """The trend point of a round whose ``metrics`` artifact is ``metrics``; ``None`` when it
    holds no readable score."""
    items = metrics.get("scores") if isinstance(metrics, Mapping) else None
    if not isinstance(items, list):
        return None
    scores = tuple(s for s in map(_score, items) if s is not None)
    return Point(round_id, number, head_sha, at, scores) if scores else None
