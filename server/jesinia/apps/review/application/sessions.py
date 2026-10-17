# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which agent session reviews which worktree, held in memory for the server's lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from jesinia.apps.review.domain.repo_access import (
    FILE_LINES_PER_SESSION,
    REPO_GIT_SECONDS_PER_SESSION,
    FileLineAllowance,
    NoReviewSessionError,
    ReadLog,
    RepoTimeBudget,
)


class ReviewSessionRegistry:
    """Binds review agent sessions to the worktree their repo tools may read.

    Review jobs bind a session when they start it and unbind it when it ends; the repo tools
    refuse any session that is not bound. Each session's per-file read allowance and its git
    time budget live here too, so they are dropped with the binding.

    What each session's tools actually read is recorded in ``reads`` for as long as it is
    bound, so a job can check its agent's claims against it before it unbinds.

    Jobs bind with ``attach``, which takes the workspace's lock for the tree's repo (set
    with ``use_tree_locks``): the prune holds the same lock while it decides what to keep,
    so a tree is never removed between being checked out and being bound.
    """

    def __init__(self) -> None:
        self._trees: dict[str, Path] = {}
        self._git_slots: dict[str, asyncio.Semaphore] = {}
        self._tree_lock: Callable[[Path], asyncio.Lock | None] | None = None
        self.allowance = FileLineAllowance(FILE_LINES_PER_SESSION)
        self.budget = RepoTimeBudget(REPO_GIT_SECONDS_PER_SESSION)
        self.reads = ReadLog()

    def use_tree_locks(self, tree_lock: Callable[[Path], asyncio.Lock | None]) -> None:
        """Have ``attach`` take ``tree_lock(path)`` (the workspace's per-repo lock)."""
        self._tree_lock = tree_lock

    async def attach(self, session_id: str, path: Path) -> None:
        """``bind`` under the tree's repo lock, so a running prune finishes with that repo
        first and the next one sees the binding.

        Raises:
            NoReviewSessionError: an empty session id.
        """
        lock = None if self._tree_lock is None else self._tree_lock(path)
        if lock is None:
            self.bind(session_id, path)
            return
        async with lock:
            self.bind(session_id, path)

    def bind(self, session_id: str, path: Path) -> None:
        """Let ``session_id`` read ``path``. Rebinding to another tree resets its allowance.

        Raises:
            NoReviewSessionError: an empty session id.
        """
        if not session_id:
            raise NoReviewSessionError("a review session needs an id")
        if self._trees.get(session_id, path) != path:
            self.allowance.drop(session_id)
            self.reads.drop(session_id)
            self.budget.drop(session_id)
        self._trees[session_id] = path
        self.budget.open(session_id)
        self._git_slots.setdefault(session_id, asyncio.Semaphore(1))

    def unbind(self, session_id: str) -> None:
        """End ``session_id``'s access and forget its allowance, its read record, its git
        time and its git slot; unknown ids are ignored."""
        self._trees.pop(session_id, None)
        self._git_slots.pop(session_id, None)
        self.budget.drop(session_id)
        self.allowance.drop(session_id)
        self.reads.drop(session_id)

    def reads_of(self, session_id: str) -> list[tuple[str, int, int]]:
        """What ``session_id``'s repo tools returned to it, as ``(path, start, end)`` line
        ranges of the reviewed head; empty once it is unbound."""
        return self.reads.ranges(session_id)

    def worktree_for(self, session_id: str) -> Path | None:
        return self._trees.get(session_id)

    def bound_trees(self) -> list[Path]:
        """Every tree a live session may read, so a prune never deletes one under it."""
        return list(self._trees.values())

    def git_slot(self, session_id: str) -> asyncio.Semaphore:
        """The session's one git slot: its repo tool calls run git one at a time.

        Slots are made only by ``bind``, so a call still in flight after ``unbind`` can't
        bring one back.

        Raises:
            NoReviewSessionError: the session isn't bound (any more).
        """
        slot = self._git_slots.get(session_id)
        if slot is None:
            raise NoReviewSessionError(f"session {session_id!r} is not reviewing a PR")
        return slot
