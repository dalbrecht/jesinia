# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the review app needs from the outside world; implemented in ``infrastructure``."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Workspace(Protocol):
    """Local clones of watched repos: one bare mirror per repo plus one detached worktree
    per reviewed head. Read-only towards GitHub: it fetches and never pushes.

    Every method validates ``repo`` (``owner/name``) and SHAs (full 40-hex) before running
    git and raises ``jesinia.apps.review.domain.workspace.WorkspaceError`` subclasses.
    """

    async def ensure_mirror(self, repo: str) -> None:
        """Create the repo's bare mirror if missing and point it at the remote."""

    async def fetch(self, repo: str, refs: list[str]) -> None:
        """Fetch full refs (``refs/pull/7/head``) into the same names, or bare SHAs."""

    async def worktree(self, repo: str, sha: str) -> Path:
        """A detached checkout of ``sha``; calling again returns the same path."""

    async def diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        """The unified diff from ``base_sha`` to ``head_sha``."""

    async def merge_base(self, repo: str, a: str, b: str) -> str:
        """The best common ancestor of two commits."""

    async def patch_ids(self, repo: str, base_sha: str, head_sha: str) -> dict[str, str]:
        """Each changed path mapped to the ``git patch-id --stable`` of its own diff, so a
        pure rebase compares equal file by file and a conflict resolution does not."""

    async def prune(self, keep: set[tuple[str, str]]) -> int:
        """Remove every worktree whose ``(repo, sha)`` is not in ``keep``; return how many."""
