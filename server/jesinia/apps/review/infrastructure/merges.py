# SPDX-License-Identifier: AGPL-3.0-or-later
"""``PrMerges`` over the github service: whether a reviewed PR landed, as which commit, and
at which head (R87, R88: the merge commit isn't "changed since" the author's review only
when it merged the head they reviewed)."""

from __future__ import annotations

from jesinia.apps.review.application.knowledge import MergeState
from jesinia.apps.review.domain.workspace import WorkspaceError, check_sha
from jesinia.services.github.application.prs import PrClient


def _sha(value: str | None) -> str | None:
    """A SHA, or ``None`` for anything that isn't one (counted as unknown)."""
    if value is None:
        return None
    try:
        return check_sha(value)
    except WorkspaceError:
        return None


class GitHubMerges:
    """A PR's merge state from GitHub."""

    def __init__(self, prs: PrClient) -> None:
        self._prs = prs

    async def merge_state(self, repo: str, number: int) -> MergeState:
        """Raises: GitHubError (any GitHub failure, as ``PrClient`` raises it)."""
        state, sha, head = await self._prs.merge_state(repo, number)
        if state == "merged":
            return MergeState("merged", _sha(sha), _sha(head))
        return MergeState("closed" if state == "closed" else "open", None)
