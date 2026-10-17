# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which agent session reviews which worktree, held in memory for the server's lifetime."""

from __future__ import annotations

from pathlib import Path

from jesinia.apps.review.domain.repo_access import (
    FILE_LINES_PER_SESSION,
    FileLineAllowance,
    NoReviewSessionError,
)


class ReviewSessionRegistry:
    """Binds review agent sessions to the worktree their repo tools may read.

    Review jobs bind a session when they start it and unbind it when it ends; the repo tools
    refuse any session that is not bound. Each session's per-file read allowance lives here
    too, so it is dropped with the binding.
    """

    def __init__(self) -> None:
        self._trees: dict[str, Path] = {}
        self.allowance = FileLineAllowance(FILE_LINES_PER_SESSION)

    def bind(self, session_id: str, path: Path) -> None:
        """Let ``session_id`` read ``path``. Rebinding to another tree resets its allowance.

        Raises:
            NoReviewSessionError: an empty session id.
        """
        if not session_id:
            raise NoReviewSessionError("a review session needs an id")
        if self._trees.get(session_id, path) != path:
            self.allowance.drop(session_id)
        self._trees[session_id] = path

    def unbind(self, session_id: str) -> None:
        """End ``session_id``'s access and forget its allowance; unknown ids are ignored."""
        self._trees.pop(session_id, None)
        self.allowance.drop(session_id)

    def worktree_for(self, session_id: str) -> Path | None:
        return self._trees.get(session_id)
