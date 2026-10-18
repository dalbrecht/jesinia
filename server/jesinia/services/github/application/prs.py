# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pull request reads for the review tool (spec §2, §9).

Every read goes through ``GitHubService.get`` (ETag-revalidated, so unchanged pages cost no
rate limit) or ``graphql`` (review threads, whose resolved flag REST does not expose).
Writes are not here: ``GitHubService.create_review`` is the only one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from jesinia.services.github.application.payload import GHOST as _GHOST
from jesinia.services.github.application.payload import PER_PAGE as PER_PAGE
from jesinia.services.github.application.payload import (
    GitHubPayloadError as GitHubPayloadError,
)
from jesinia.services.github.application.payload import account as _account
from jesinia.services.github.application.payload import flag as _flag
from jesinia.services.github.application.payload import get_json, get_pages
from jesinia.services.github.application.payload import list_of as _list
from jesinia.services.github.application.payload import mapping as _mapping
from jesinia.services.github.application.payload import number as _number
from jesinia.services.github.application.payload import optional_mapping as _optional_mapping
from jesinia.services.github.application.payload import optional_number as _optional_number
from jesinia.services.github.application.payload import optional_text as _optional_text
from jesinia.services.github.application.payload import optional_time as _optional_time
from jesinia.services.github.application.payload import text as _text
from jesinia.services.github.application.service import (
    API,
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubService,
)
from jesinia.services.github.domain.pr import (
    Check,
    CheckState,
    InvalidPullRequestReferenceError,
    PrCommit,
    PrFile,
    PullRequest,
    ReviewSummary,
    ReviewThread,
    check_status_from_commit_status,
    check_status_from_run,
    rollup_checks,
    validate_pull_number,
    validate_repo,
    validate_sha,
)

log = logging.getLogger(__name__)

# GitHub's search rejects queries longer than 256 characters.
SEARCH_QUERY_LIMIT = 256
# Search returns at most 1000 results (10 pages of 100); asking for page 11 is a 422.
SEARCH_MAX_PAGES = 10
# Cap on concurrent per-PR fetches while building the inbox.
INBOX_CONCURRENCY = 8
# Who-qualifiers in inbox priority order: requested reviews first (spec §2 "Order").
_REQUESTED = "review-requested:@me"
_INBOX_QUALIFIERS = (_REQUESTED, "author:@me", "reviewed-by:@me")

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved path line originalLine originalStartLine diffSide
          comments(first: 1) {
            nodes { body author { login } commit { oid } originalCommit { oid } }
          }
        }
      }
    }
  }
}
"""


_SearchHit = tuple[tuple[str, int], Mapping[str, object]]


def _search_queries(qualifier: str, repos: Sequence[str]) -> list[str]:
    base = f"is:pr is:open {qualifier}"
    queries: list[str] = []
    current = base
    for repo in repos:
        term = f" repo:{repo}"
        if current != base and len(current) + len(term) > SEARCH_QUERY_LIMIT:
            queries.append(current)
            current = base
        current += term
    queries.append(current)
    return queries


def _payload_repo(repo: str, what: str) -> str:
    """A repo name GitHub sent, checked before it can reach a request path or a key."""
    try:
        return validate_repo(repo)
    except InvalidPullRequestReferenceError as exc:
        raise GitHubPayloadError(f"unexpected {what}: {exc}") from exc


def _repo_of_search_item(item: Mapping[str, object]) -> str:
    """``owner/name`` from a hit's ``https://api.github.com/repos/{owner}/{name}``."""
    url = _text(item, "repository_url")
    prefix = f"{API}/repos/"
    if not url.startswith(prefix):
        raise GitHubPayloadError(f"unexpected repository_url in a search hit: {url!r}")
    return _payload_repo(url.removeprefix(prefix), "repository_url in a search hit")


def _pull_request(repo: str, data: Mapping[str, object], viewer: str) -> PullRequest:
    author, author_is_bot = _account(data, "user")
    head = _mapping(data.get("head"), "head")
    base = _mapping(data.get("base"), "base")
    head_repo = _optional_mapping(head, "repo")
    requested = (
        _optional_text(_mapping(r, "requested reviewer"), "login")
        for r in _list(data.get("requested_reviewers", []), "requested_reviewers")
    )
    state = "merged" if data.get("merged_at") else _text(data, "state")
    base_repo = _optional_mapping(base, "repo")
    return PullRequest(
        # GitHub's own spelling: the caller's may differ in case, and queue keys must not.
        repo=repo
        if base_repo is None
        else _payload_repo(_text(base_repo, "full_name"), "base repo"),
        number=_number(data, "number"),
        title=_text(data, "title"),
        body=_optional_text(data, "body") or "",
        author=author,
        author_is_bot=author_is_bot,
        base_ref=_text(base, "ref"),
        head_ref=_text(head, "ref"),
        head_sha=_text(head, "sha"),
        base_sha=_text(base, "sha"),
        head_repo=None if head_repo is None else _text(head_repo, "full_name"),
        state=state,
        draft=_flag(data, "draft"),
        additions=_number(data, "additions"),
        deletions=_number(data, "deletions"),
        changed_files=_number(data, "changed_files"),
        commit_count=_number(data, "commits"),
        url=_text(data, "html_url"),
        review_requested=viewer in requested,
        mergeable_state=_optional_text(data, "mergeable_state"),
        created_at=datetime.fromisoformat(_text(data, "created_at")),
    )


def _pull_request_from_search(
    repo: str, item: Mapping[str, object], fetch_error: str
) -> PullRequest:
    """What a search hit alone says about a PR whose own fetch failed."""
    author, author_is_bot = _account(item, "user")
    return PullRequest(
        repo=repo,  # from the hit's repository_url: already GitHub's spelling
        number=_number(item, "number"),
        title=_text(item, "title"),
        body=_optional_text(item, "body") or "",
        author=author,
        author_is_bot=author_is_bot,
        base_ref="",
        head_ref="",
        head_sha="",
        base_sha="",
        head_repo=None,
        state=_text(item, "state"),
        draft=item.get("draft") is True,
        additions=0,
        deletions=0,
        changed_files=0,
        url=_text(item, "html_url"),
        review_requested=False,
        mergeable_state=None,
        created_at=datetime.fromisoformat(_text(item, "created_at")),
        fetch_error=fetch_error,
    )


def _check_run(data: Mapping[str, object]) -> Check:
    status = check_status_from_run(_text(data, "status"), _optional_text(data, "conclusion"))
    return Check(_text(data, "name"), status, _optional_text(data, "html_url"))


def _commit_status(data: Mapping[str, object]) -> Check:
    status = check_status_from_commit_status(_text(data, "state"))
    return Check(_text(data, "context"), status, _optional_text(data, "target_url"))


def _thread(node: Mapping[str, object]) -> ReviewThread:
    comments = _mapping(node.get("comments"), "comments")
    first_comments = _list(comments.get("nodes"), "comments.nodes")
    if not first_comments:
        raise GitHubPayloadError("a review thread came back without its first comment")
    first = _mapping(first_comments[0], "comment")
    author = _optional_mapping(first, "author")
    # Pair the line with its commit: the current position while the thread still applies,
    # else where it was first written.
    line = _optional_number(node, "line")
    commit = _optional_mapping(first, "commit")
    if line is None:
        line = _optional_number(node, "originalLine")
        commit = _optional_mapping(first, "originalCommit")
    original = _optional_mapping(first, "originalCommit")
    side = node.get("diffSide")
    return ReviewThread(
        path=_text(node, "path"),
        line=line,
        body=_text(first, "body"),
        author=_GHOST if author is None else _text(author, "login"),
        resolved=_flag(node, "isResolved"),
        commit_sha=None if commit is None else _text(commit, "oid"),
        original_commit_sha=None if original is None else _text(original, "oid"),
        original_line=_optional_number(node, "originalLine"),
        original_start_line=_optional_number(node, "originalStartLine"),
        side="LEFT" if side == "LEFT" else "RIGHT" if side == "RIGHT" else None,
    )


class PrClient:
    """Typed pull request reads over a ``GitHubService``.

    Raises (every method): ``GitHubUnavailable`` without a usable token,
    ``GitHubRequestError`` with GitHub's message on a non-2xx answer,
    ``GitHubPayloadError`` on an unexpected shape, and ``InvalidPullRequestReferenceError``
    for a malformed repo, number or SHA.
    """

    def __init__(self, github: GitHubService) -> None:
        self._github = github

    async def _get_json(self, path: str, params: Mapping[str, str] | None = None) -> object:
        return await get_json(self._github, path, params)

    async def _get_pages(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        items_key: str | None = None,
        max_pages: int | None = None,
    ) -> list[Mapping[str, object]]:
        return await get_pages(self._github, path, params, items_key, max_pages)

    async def viewer(self) -> str:
        """The login the token belongs to."""
        return _text(_mapping(await self._get_json("/user"), "/user"), "login")

    async def _pull(self, repo: str, number: int, viewer: str) -> PullRequest:
        path = f"/repos/{repo}/pulls/{number}"
        return _pull_request(repo, _mapping(await self._get_json(path), path), viewer)

    async def get(self, repo: str, number: int) -> PullRequest:
        """One PR; ``review_requested`` when the viewer is a requested reviewer by name."""
        validate_repo(repo)
        validate_pull_number(number)
        return await self._pull(repo, number, await self.viewer())

    async def _search(self, qualifier: str, repos: Sequence[str]) -> list[_SearchHit]:
        found: list[_SearchHit] = []
        for query in _search_queries(qualifier, repos):
            items = await self._get_pages(
                "/search/issues", {"q": query}, items_key="items", max_pages=SEARCH_MAX_PAGES
            )
            found.extend(
                ((_repo_of_search_item(item), _number(item, "number")), item) for item in items
            )
        return found

    async def _inbox_pull(
        self, hit: _SearchHit, viewer: str, limit: asyncio.Semaphore
    ) -> PullRequest:
        """The full PR, or, if that one fetch fails, the search hit carrying the cause, so
        one inaccessible PR never empties the inbox. A rate limit is not one PR's problem:
        it is raised, so the caller backs off instead of caching a page of empty rows."""
        (repo, number), item = hit
        try:
            async with limit:
                return await self._pull(repo, number, viewer)
        except GitHubRateLimitedError:
            raise
        except (GitHubRequestError, GitHubPayloadError) as exc:
            return _pull_request_from_search(repo, item, str(exc))

    async def viewer_emails(self) -> list[str]:
        """The viewer's email addresses, lowercase: the profile's public email, GitHub's
        ``noreply`` addresses for the account, and every verified address ``/user/emails``
        lists when the token may read it (the ``user:email`` scope; without it, the rest)."""
        user = _mapping(await self._get_json("/user"), "/user")
        login = _text(user, "login")
        emails = [f"{login}@users.noreply.github.com"]
        if isinstance(user.get("id"), int):
            emails.append(f"{user['id']}+{login}@users.noreply.github.com")
        if isinstance(public := user.get("email"), str) and public:
            emails.append(public)
        try:
            listed = await self._get_pages("/user/emails")
        except GitHubRequestError as exc:
            if exc.status not in (403, 404):
                raise
            listed = []  # the token can't read them: the public ones still match
        emails += [
            address
            for item in listed
            if item.get("verified") is True and isinstance(address := item.get("email"), str)
        ]
        return list(dict.fromkeys(email.lower() for email in emails))

    async def viewer_orgs(self) -> list[str]:
        """Logins of the orgs the viewer belongs to, lowercase (every page)."""
        return [_text(org, "login").lower() for org in await self._get_pages("/user/orgs")]

    async def inbox_repos(self) -> list[str]:
        """Every repo, across GitHub, with an open PR whose review is requested of the viewer
        or that the viewer wrote; lowercase ``owner/name``, sorted. Seeds the watched repos."""
        qualifiers = (f"{_REQUESTED} archived:false", "author:@me archived:false")
        found = await asyncio.gather(*(self._search(q, []) for q in qualifiers))
        return sorted({repo.lower() for hits in found for (repo, _), _ in hits})

    async def list_review_inbox(self, repos: list[str]) -> list[PullRequest]:
        """Open PRs in ``repos`` whose review is requested of the viewer (directly or via a
        team), that they authored, or that they reviewed; requested ones first, no repeats."""
        for repo in repos:
            validate_repo(repo)
        if not repos:
            return []
        found = await asyncio.gather(*(self._search(q, repos) for q in _INBOX_QUALIFIERS))
        requested = {key for key, _ in found[0]}
        hits: dict[tuple[str, int], Mapping[str, object]] = {}
        for key, item in (hit for hits_of_one in found for hit in hits_of_one):
            hits.setdefault(key, item)
        if not hits:
            return []
        viewer = await self.viewer()
        limit = asyncio.Semaphore(INBOX_CONCURRENCY)
        pulls = await asyncio.gather(*(self._inbox_pull(h, viewer, limit) for h in hits.items()))
        return [
            replace(pr, review_requested=pr.review_requested or key in requested)
            for key, pr in zip(hits, pulls, strict=True)
        ]

    async def files(self, repo: str, number: int) -> list[PrFile]:
        path = f"/repos/{validate_repo(repo)}/pulls/{validate_pull_number(number)}/files"
        return [
            PrFile(
                filename=_text(item, "filename"),
                status=_text(item, "status"),
                additions=_number(item, "additions"),
                deletions=_number(item, "deletions"),
                patch=_optional_text(item, "patch"),
                previous_filename=_optional_text(item, "previous_filename"),
            )
            for item in await self._get_pages(path)
        ]

    async def files_of(self, pr: PullRequest) -> tuple[PullRequest, list[PrFile]]:
        """``pr``'s files, and ``pr`` with ``files_truncated`` set when GitHub listed fewer
        than ``changed_files``: its files endpoint stops at 3,000, and a short last page
        otherwise looks like the whole change."""
        files = await self.files(pr.repo, pr.number)
        truncated = pr.changed_files > len(files)
        if truncated:
            log.warning(
                "github.pr file list truncated: %s#%d changes %d files, GitHub listed %d",
                pr.repo,
                pr.number,
                pr.changed_files,
                len(files),
            )
        return replace(pr, files_truncated=truncated), files

    async def commits(self, repo: str, number: int) -> list[PrCommit]:
        """The PR's commits, oldest first; ``author`` is the GitHub login when the commit is
        linked to an account, else the git author name."""
        path = f"/repos/{validate_repo(repo)}/pulls/{validate_pull_number(number)}/commits"
        commits: list[PrCommit] = []
        for item in await self._get_pages(path):
            commit = _mapping(item.get("commit"), "commit")
            git_author = _mapping(commit.get("author"), "commit.author")
            account = _optional_mapping(item, "author")
            author = _text(account, "login") if account else _text(git_author, "name")
            commits.append(
                PrCommit(
                    sha=_text(item, "sha"),
                    message=_text(commit, "message"),
                    author=author,
                    authored_at=_optional_time(git_author, "date"),
                )
            )
        return commits

    async def commits_of(self, pr: PullRequest) -> tuple[PullRequest, list[PrCommit]]:
        """``pr``'s commits, and ``pr`` with ``commits_truncated`` set when GitHub listed
        fewer than the PR has: its commits endpoint stops at 250, and a short last page
        otherwise looks like the whole history."""
        commits = await self.commits(pr.repo, pr.number)
        truncated = pr.commit_count > len(commits)
        if truncated:
            log.warning(
                "github.pr commit list truncated: %s#%d has %d commits, GitHub listed %d",
                pr.repo,
                pr.number,
                pr.commit_count,
                len(commits),
            )
        return replace(pr, commits_truncated=truncated), commits

    async def checks(self, repo: str, sha: str) -> CheckState:
        """Check runs plus legacy commit statuses on ``sha``, rolled up into one state."""
        base = f"/repos/{validate_repo(repo)}/commits/{validate_sha(sha)}"
        # The combined status already keeps only the latest status per context, but it
        # pages like any list, so a failure past the first page must still be read.
        runs, statuses = await asyncio.gather(
            self._get_pages(f"{base}/check-runs", items_key="check_runs"),
            self._get_pages(f"{base}/status", items_key="statuses"),
        )
        checks = [_check_run(run) for run in runs]
        checks += [_commit_status(status) for status in statuses]
        return rollup_checks(checks)

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]:
        """Submitted (and the viewer's pending) reviews with their inline comment counts."""
        base = f"/repos/{validate_repo(repo)}/pulls/{validate_pull_number(number)}"
        reviews, comments = await asyncio.gather(
            self._get_pages(f"{base}/reviews"), self._get_pages(f"{base}/comments")
        )
        counts: dict[int, int] = {}
        for comment in comments:
            review_id = _optional_number(comment, "pull_request_review_id")
            if review_id is not None:
                counts[review_id] = counts.get(review_id, 0) + 1
        summaries: list[ReviewSummary] = []
        for review in reviews:
            author, author_is_bot = _account(review, "user")
            summaries.append(
                ReviewSummary(
                    author=author,
                    author_is_bot=author_is_bot,
                    state=_text(review, "state"),
                    commit_sha=_optional_text(review, "commit_id"),
                    submitted_at=_optional_time(review, "submitted_at"),
                    comment_count=counts.get(_number(review, "id"), 0),
                    body=_optional_text(review, "body") or "",
                    url=_optional_text(review, "html_url"),
                )
            )
        return summaries

    async def threads(self, repo: str, number: int) -> list[ReviewThread]:
        """Every review thread with its first comment, following GraphQL cursors."""
        owner, name = validate_repo(repo).split("/")
        validate_pull_number(number)
        threads: list[ReviewThread] = []
        after: str | None = None
        while True:
            data = await self._github.graphql(
                _THREADS_QUERY, {"owner": owner, "name": name, "number": number, "after": after}
            )
            repository = _mapping(data.get("repository"), "repository")
            pull = _mapping(repository.get("pullRequest"), "pullRequest")
            connection = _mapping(pull.get("reviewThreads"), "reviewThreads")
            nodes = _list(connection.get("nodes"), "reviewThreads.nodes")
            threads.extend(_thread(_mapping(node, "thread")) for node in nodes)
            page_info = _mapping(connection.get("pageInfo"), "pageInfo")
            if not _flag(page_info, "hasNextPage"):
                return threads
            after = _text(page_info, "endCursor")
