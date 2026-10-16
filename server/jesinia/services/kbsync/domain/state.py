# SPDX-License-Identifier: AGPL-3.0-or-later
"""The KB repository's sync-relevant state (spec §4.4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["off", "commit", "push"]
Problem = Literal["detached-head", "rebase-in-progress", "merge-in-progress", "no-upstream"]

PROBLEM_TEXT: dict[Problem, str] = {
    "detached-head": "Detached HEAD: check out a branch to resume syncing.",
    "rebase-in-progress": "A rebase is in progress: finish or abort it in a terminal.",
    "merge-in-progress": "A merge is in progress: finish or abort it in a terminal.",
    "no-upstream": "No upstream branch: add a remote/upstream to enable push.",
}

# Problems that stop a sync cycle without touching the repository.
BLOCKING: frozenset[Problem] = frozenset(
    {"detached-head", "rebase-in-progress", "merge-in-progress"}
)


@dataclass(frozen=True)
class GitState:
    mode: Mode
    branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    dirty: bool
    problem: Problem | None

    @property
    def problem_text(self) -> str | None:
        return PROBLEM_TEXT[self.problem] if self.problem else None

    @property
    def blocked(self) -> bool:
        return self.problem in BLOCKING
