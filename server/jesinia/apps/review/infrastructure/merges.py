# SPDX-License-Identifier: AGPL-3.0-or-later
"""``PrMerges`` over the github service: whether a reviewed PR landed, and as which commit
(R87: that commit isn't "changed since" the author's review of it)."""

from __future__ import annotations

from jesinia.apps.review.application.knowledge import MergeState
from jesinia.apps.review.domain.workspace import WorkspaceError, check_sha
from jesinia.services.github.application.prs import PrClient


class GitHubMerges:
    """A PR's merge state from GitHub; a merge commit that isn't a SHA counts as unknown."""

    def __init__(self, prs: PrClient) -> None:
        self._prs = prs

    async def merge_state(self, repo: str, number: int) -> MergeState:
        """Raises: GitHubError (any GitHub failure, as ``PrClient`` raises it)."""
        state, sha = await self._prs.merge_state(repo, number)
        if state == "merged":
            try:
                return MergeState("merged", None if sha is None else check_sha(sha))
            except WorkspaceError:
                return MergeState("merged", None)
        return MergeState("closed" if state == "closed" else "open", None)
