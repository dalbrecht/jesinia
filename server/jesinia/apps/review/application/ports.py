# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the review app needs from the outside world; implemented in ``infrastructure``."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from jesinia.apps.review.domain.repo_access import CappedOutput, RegexDialect


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


class TreeGit(Protocol):
    """Read-only git inside a review worktree, for the repo tools.

    Runs with the workspace's hardened environment and without the system config (local
    reads need no credentials). Raises ``WorkspaceError`` subclasses, including for a
    ``tree`` outside the workspace.
    """

    async def read_in_tree(
        self,
        tree: Path,
        args: Sequence[str],
        *,
        output_limit: int,
        glob_pathspecs: bool = False,
        accept_exit_codes: frozenset[int] = frozenset({0}),
        without_worktree: bool = False,
        timeout: float | None = None,
    ) -> CappedOutput:
        """Run ``git <args>`` in ``tree``; stdout is cut (and the process killed) past
        ``output_limit`` bytes. ``glob_pathspecs`` lets ``:(glob)`` pathspecs work, which
        are otherwise literal. An exit code outside ``accept_exit_codes`` raises.
        ``without_worktree`` runs against the tree's repository only, so no file in the
        checkout (``.mailmap``) is read; revisions must then be full commit ids.
        ``timeout`` overrides the workspace's default time limit, in seconds."""

    async def regex_dialect(self) -> RegexDialect:
        """Which ERE extensions the installed git's regex library supports (probed once)."""
