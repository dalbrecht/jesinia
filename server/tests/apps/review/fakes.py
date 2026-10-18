# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test doubles for the review queue: a settable clock and an in-memory GitHub."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from jesinia.services.github.application.service import GitHubRequestError
from jesinia.services.github.domain.pr import (
    CheckState,
    PrFile,
    PullRequest,
    ReviewSummary,
    ReviewThread,
    RollupState,
)

START = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
HEAD = "a" * 40
BASE = "b" * 40


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


def pull(
    number: int = 1,
    repo: str = "acme/web",
    *,
    head: str = HEAD,
    base: str = BASE,
    title: str | None = None,
    author: str = "alice",
    bot: bool = False,
    requested: bool = False,
    additions: int = 10,
    deletions: int = 5,
    files: int = 2,
    state: str = "open",
    created: datetime = START - timedelta(days=1),
    fetch_error: str | None = None,
) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=number,
        title=title or f"PR {number}",
        body="",
        author=author,
        author_is_bot=bot,
        base_ref="" if fetch_error else "main",
        head_ref="" if fetch_error else f"feature-{number}",
        head_sha="" if fetch_error else head,
        base_sha="" if fetch_error else base,
        head_repo=None if fetch_error else repo,
        state=state,
        draft=False,
        additions=0 if fetch_error else additions,
        deletions=0 if fetch_error else deletions,
        changed_files=0 if fetch_error else files,
        url=f"https://github.com/{repo}/pull/{number}",
        review_requested=requested,
        mergeable_state=None,
        created_at=created,
        fetch_error=fetch_error,
    )


def review(author: str, *, bot: bool = False, sha: str = HEAD, comments: int = 0) -> ReviewSummary:
    return ReviewSummary(
        author=author,
        author_is_bot=bot,
        state="COMMENTED",
        commit_sha=sha,
        submitted_at=START,
        comment_count=comments,
    )


def _key(repo: str, number: int) -> tuple[str, int]:
    return repo.lower(), number


class FakePullRequests:
    """GitHub as the queue sees it. ``error`` fails every call; ``detail_errors`` fail the
    checks of one PR; ``get_error`` fails single-PR reads; ``hold`` parks
    ``list_review_inbox`` until it is set."""

    def __init__(self) -> None:
        self.inbox: list[PullRequest] = []
        self.others: dict[tuple[str, int], PullRequest] = {}
        self.ci: dict[str, RollupState] = {}
        self.reviews_of: dict[tuple[str, int], list[ReviewSummary]] = {}
        self.threads_of: dict[tuple[str, int], list[ReviewThread]] = {}
        self.error: Exception | None = None
        self.detail_errors: dict[tuple[str, int], Exception] = {}
        self.get_error: Exception | None = None
        self.orgs: list[str] = []
        self.org_calls = 0
        # Head SHAs single-PR reads report, one per call, before falling back to the PR's own.
        self.heads_seen_by_get: list[str] = []
        # Checks on these head SHAs park until their event is set.
        self.detail_holds: dict[str, asyncio.Event] = {}
        self.hold: asyncio.Event | None = None
        self.inbox_calls: list[list[str]] = []
        self.file_calls = 0

    def _fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def viewer(self) -> str:
        self._fail()
        return "me"

    async def viewer_orgs(self) -> list[str]:
        self._fail()
        self.org_calls += 1
        return list(self.orgs)

    async def inbox_repos(self) -> list[str]:
        """Repos with a PR whose review is requested of "me", or that "me" wrote."""
        self._fail()
        mine = {pr.repo.lower() for pr in self.inbox if pr.review_requested or pr.author == "me"}
        return sorted(mine)

    async def list_review_inbox(self, repos: list[str]) -> list[PullRequest]:
        self.inbox_calls.append(list(repos))
        if self.hold is not None:
            await self.hold.wait()
        self._fail()
        return [pr for pr in self.inbox if pr.repo.lower() in repos]

    async def get(self, repo: str, number: int) -> PullRequest:
        self._fail()
        if self.get_error is not None:
            raise self.get_error
        for pr in [*self.inbox, *self.others.values()]:
            if _key(pr.repo, pr.number) == _key(repo, number):
                if self.heads_seen_by_get:
                    return replace(pr, head_sha=self.heads_seen_by_get.pop(0))
                return pr
        raise GitHubRequestError(404, "Not Found")

    async def checks(self, repo: str, sha: str) -> CheckState:
        self._fail()
        if sha in self.detail_holds:
            await self.detail_holds[sha].wait()
        for (failing_repo, number), exc in self.detail_errors.items():
            pr = await self.get(repo, number)
            if failing_repo == repo.lower() and pr.head_sha == sha:
                raise exc
        return CheckState(self.ci.get(sha, "passing"), [])

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]:
        self._fail()
        return self.reviews_of.get(_key(repo, number), [])

    async def threads(self, repo: str, number: int) -> list[ReviewThread]:
        self._fail()
        return self.threads_of.get(_key(repo, number), [])

    async def files(self, repo: str, number: int) -> list[PrFile]:
        self._fail()
        self.file_calls += 1
        return [PrFile("src/app.py", "modified", 3, 1, "@@ -1 +1 @@\n-a\n+b", None)]
