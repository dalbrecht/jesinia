# SPDX-License-Identifier: AGPL-3.0-or-later
"""Completeness (spec §2, §6.2): what triage works through.

An issue is complete when readiness, priority and type each have exactly one value and it has
at least one area. An epic or an initiative needs priority only: an epic's readiness is derived
from its children, and neither needs a type or an area. A conflict (two values where one is
allowed, or both hierarchy labels) makes an issue incomplete without counting as missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from jesinia.apps.issues.domain.schema import AREA, HIERARCHY, PRIORITY, READINESS, TYPE, Classified

_ISSUE_NEEDS = (READINESS, PRIORITY, TYPE, AREA)
_PARENT_NEEDS = (PRIORITY,)


@dataclass(frozen=True)
class Completeness:
    """Whether an issue's label set is complete; ``missing`` names dimensions in schema order
    and ``conflicts`` describes each conflict as ``"dimension: a, b"``."""

    complete: bool
    missing: list[str]
    conflicts: list[str]


def completeness(classified: Classified, is_epic: bool) -> Completeness:
    """Check ``classified`` against the dimensions its kind needs.

    ``is_epic`` is true when the issue has sub-issues (spec §6.2: the ``epic`` label **or**
    sub-issues make an epic); the epic and initiative labels count on their own.
    """
    is_parent = is_epic or classified.is_epic or classified.is_initiative
    needs = _PARENT_NEEDS if is_parent else _ISSUE_NEEDS
    conflicts = [
        c for c in classified.conflicts if c.dimension in needs or c.dimension == HIERARCHY
    ]
    conflicted = {c.dimension for c in conflicts}
    present = {
        READINESS: classified.readiness is not None,
        PRIORITY: classified.priority is not None,
        TYPE: classified.type is not None,
        AREA: bool(classified.areas),
    }
    missing = [d for d in needs if not present[d] and d not in conflicted]
    return Completeness(
        complete=not missing and not conflicts,
        missing=missing,
        conflicts=[c.describe() for c in conflicts],
    )
