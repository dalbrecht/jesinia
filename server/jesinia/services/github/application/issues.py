# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issue, label, milestone and sub-issue reads and writes for the Issues app (spec §7).

Reads go through ``GitHubService.get`` (ETag-revalidated); writes go through
``GitHubService.write``, the one write path the review tool's posting also uses. The
client has no notion of principals: the owner check lives on the routes, which refuse
session principals before calling a write.

Issue and comment text is untrusted. It is returned verbatim and never logged; the app
fences it as ``UntrustedDoc``s before any agent sees it.

GitHub silently **creates** an unknown label when one is added to an issue or named in a new
issue. No label may ever be created implicitly (plan "The apply engine"), so callers of
``add_labels`` and ``create_issue`` must check the names against ``labels(repo)`` first.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from datetime import datetime

from jesinia.services.github.application.payload import (
    PER_PAGE,
    GitHubPayloadError,
    account,
    get_json,
    get_page,
    get_pages,
    iso_time,
    list_of,
    mapping,
    number,
    optional_mapping,
    optional_number,
    optional_text,
    optional_time,
    text,
)
from jesinia.services.github.application.service import (
    API,
    GitHubRequestError,
    GitHubService,
)
from jesinia.services.github.domain.issue import (
    CloseReason,
    CommentRef,
    InvalidLabelError,
    Issue,
    IssueComment,
    IssueRef,
    Label,
    LabeledItem,
    LinkedPr,
    Milestone,
    Page,
    TimelineEvent,
    github_time,
    validate_close,
    validate_comment_body,
    validate_content_path,
    validate_database_id,
    validate_issue_body,
    validate_issue_number,
    validate_issue_title,
    validate_label_color,
    validate_label_description,
    validate_label_filter,
    validate_label_name,
    validate_label_names,
    validate_page_token,
)
from jesinia.services.github.domain.repo import InvalidRepoError, path_segment, validate_repo

__all__ = ["PER_PAGE", "IssueClient"]

# The timeline events the issue view and suggestion context keep: labels, close and reopen,
# and references from commits, issues and PRs.
TIMELINE_EVENTS: frozenset[str] = frozenset(
    {"labeled", "unlabeled", "closed", "reopened", "referenced", "cross-referenced"}
)
_REPOS_PREFIX = f"{API}/repos/"


def _label_segment(name: str) -> str:
    """A label name as one encoded path segment: ``priority: high`` → ``priority%3A%20high``."""
    return path_segment(validate_label_name(name))


def _issue_path(repo: str, number_: int) -> str:
    return f"/repos/{validate_repo(repo)}/issues/{validate_issue_number(number_)}"


def _repo_from_api_url(url: str, what: str) -> tuple[str, str]:
    """(``owner/name``, rest) of ``https://api.github.com/repos/{owner}/{name}{rest}``."""
    if not url.startswith(_REPOS_PREFIX):
        raise GitHubPayloadError(f"unexpected {what}: {url!r}")
    owner, _, remainder = url.removeprefix(_REPOS_PREFIX).partition("/")
    name, slash, rest = remainder.partition("/")
    try:
        return validate_repo(f"{owner}/{name}"), slash + rest
    except InvalidRepoError as exc:
        raise GitHubPayloadError(f"unexpected {what}: {exc}") from exc


def _issue_ref_from_url(url: str) -> IssueRef:
    repo, rest = _repo_from_api_url(url, "parent_issue_url")
    issue_number = rest.removeprefix("/issues/")
    if not issue_number.isdigit():
        raise GitHubPayloadError(f"unexpected parent_issue_url: {url!r}")
    return IssueRef(repo, int(issue_number))


def _label_names(data: Mapping[str, object]) -> tuple[str, ...]:
    labels = list_of(data.get("labels", []), "labels")
    return tuple(
        label if isinstance(label, str) else text(mapping(label, "label"), "name")
        for label in labels
    )


def _issue(repo: str, data: Mapping[str, object]) -> Issue:
    repository_url = optional_text(data, "repository_url")
    if repository_url is not None:
        # GitHub's spelling, and the right repo for a sub-issue that lives elsewhere.
        repo = _repo_from_api_url(repository_url, "repository_url")[0]
    state = text(data, "state")
    if state not in ("open", "closed"):
        raise GitHubPayloadError(f"unexpected issue state {state!r}")
    milestone = optional_mapping(data, "milestone")
    parent_url = optional_text(data, "parent_issue_url")
    summary = optional_mapping(data, "sub_issues_summary")
    return Issue(
        repo=repo,
        number=number(data, "number"),
        id=number(data, "id"),
        title=text(data, "title"),
        body=optional_text(data, "body") or "",
        state="open" if state == "open" else "closed",
        state_reason=optional_text(data, "state_reason"),
        labels=_label_names(data),
        milestone=None if milestone is None else text(milestone, "title"),
        parent=None if parent_url is None else _issue_ref_from_url(parent_url),
        sub_issues=(),
        created_at=iso_time(data, "created_at"),
        updated_at=iso_time(data, "updated_at"),
        url=text(data, "html_url"),
        author=account(data, "user")[0],
        sub_issue_count=0 if summary is None else optional_number(summary, "total") or 0,
    )


def _label(data: Mapping[str, object]) -> Label:
    return Label(text(data, "name"), text(data, "color"), optional_text(data, "description") or "")


def _comment(data: Mapping[str, object]) -> IssueComment:
    return IssueComment(
        id=number(data, "id"),
        author=account(data, "user")[0],
        body=optional_text(data, "body") or "",
        created_at=iso_time(data, "created_at"),
        url=text(data, "html_url"),
    )


def _comment_ref(data: object) -> CommentRef:
    created = mapping(data, "comment")
    return CommentRef(number(created, "id"), text(created, "html_url"))


def _source_issue(event: Mapping[str, object]) -> Mapping[str, object] | None:
    source = optional_mapping(event, "source")
    return None if source is None else optional_mapping(source, "issue")


def _timeline_event(data: Mapping[str, object]) -> TimelineEvent:
    label = optional_mapping(data, "label")
    source = _source_issue(data)
    return TimelineEvent(
        id=optional_number(data, "id"),
        event=text(data, "event"),
        actor=account(data, "actor")[0],
        created_at=optional_time(data, "created_at"),
        label=None if label is None else text(label, "name"),
        state_reason=optional_text(data, "state_reason"),
        source=None
        if source is None
        else IssueRef(
            _repo_from_api_url(text(source, "repository_url"), "repository_url")[0],
            number(source, "number"),
        ),
        commit_id=optional_text(data, "commit_id"),
    )


def _linked_pr(data: Mapping[str, object]) -> LinkedPr | None:
    """The PR behind a ``cross-referenced`` event, or ``None`` when an issue referenced it."""
    source = _source_issue(data)
    pull = None if source is None else optional_mapping(source, "pull_request")
    if source is None or pull is None:
        return None
    repo = _repo_from_api_url(text(source, "repository_url"), "repository_url")[0]
    state = "merged" if pull.get("merged_at") else text(source, "state")
    return LinkedPr(repo, number(source, "number"), state, text(source, "html_url"))


def _decode_file(path: str, data: object) -> str:
    content = mapping(data, path)
    if optional_text(content, "type") != "file":
        raise GitHubPayloadError(f"{path} is not a file")
    if optional_text(content, "encoding") != "base64":
        # GitHub sends encoding "none" for files over 1 MB through this API.
        raise GitHubPayloadError(f"{path} came back without base64 encoding (over 1 MB?)")
    try:
        raw = base64.b64decode(text(content, "content"), validate=False)
    except binascii.Error as exc:
        raise GitHubPayloadError(f"{path} is not valid base64: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubPayloadError(f"{path} is not UTF-8 text: {exc.reason}") from exc


class IssueClient:
    """Typed issue reads and writes over a ``GitHubService``.

    Raises (every method): ``GitHubUnavailable`` without a usable token,
    ``GitHubRateLimitedError`` while rate limited, ``GitHubRequestError`` with GitHub's
    message on any other non-2xx answer, ``GitHubPayloadError`` on an unexpected shape,
    ``InvalidRepoError`` for a malformed repo and an ``IssueDomainError``
    subclass for any other invalid argument, the last two before any request is made.
    """

    def __init__(self, github: GitHubService) -> None:
        self._github = github

    async def _page(
        self,
        path: str,
        page_token: str | None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[list[Mapping[str, object]], str | None]:
        page = validate_page_token(page_token)
        items, has_more = await get_page(self._github, path, page, params)
        return items, str(page + 1) if has_more else None

    # Reads

    async def list_issues(
        self, repo: str, since: datetime | None, page_token: str | None
    ) -> Page[Issue]:
        """One page of the repo's issues, open and closed, least recently updated first, so
        a sync can resume from the last ``updated_at``. Pull requests are dropped, so a page
        may hold fewer than ``PER_PAGE`` issues and still have a next one. ``parent`` comes
        from the payload when GitHub includes it; ``sub_issues`` stays empty (only counts
        come back, as ``sub_issue_count``)."""
        path = f"/repos/{validate_repo(repo)}/issues"
        params = {"state": "all", "sort": "updated", "direction": "asc"}
        if since is not None:
            params["since"] = github_time(since)
        items, next_token = await self._page(path, page_token, params)
        issues = [_issue(repo, item) for item in items if "pull_request" not in item]
        return Page(issues, next_token)

    async def repository(self, repo: str) -> str:
        """GitHub's own ``owner/name`` for ``repo`` (GitHub answers any letter case), which
        also proves the repo exists and the token can read it."""
        path = f"/repos/{validate_repo(repo)}"
        return text(mapping(await get_json(self._github, path), path), "full_name")

    async def labeled(self, repo: str, name: str) -> list[LabeledItem]:
        """Every issue and pull request carrying label ``name`` now, open and closed (every
        page). Pull requests are kept and marked: deleting a label strips it from them too.

        Raises:
            InvalidLabelError: ``name`` has a comma, which GitHub's filter can't express.
        """
        path = f"/repos/{validate_repo(repo)}/issues"
        params = {"labels": validate_label_filter(name), "state": "all"}
        return [
            LabeledItem(number(item, "number"), _label_names(item), "pull_request" in item)
            for item in await get_pages(self._github, path, params)
        ]

    async def get(self, repo: str, n: int) -> Issue:
        """One issue, from one request; its sub-issues are ``sub_issues(repo, n)``."""
        path = _issue_path(repo, n)
        data = mapping(await get_json(self._github, path), path)
        if "pull_request" in data:
            raise GitHubPayloadError(f"{repo}#{n} is a pull request, not an issue")
        return _issue(repo, data)

    async def parent(self, repo: str, n: int) -> Issue | None:
        """The issue's parent (with its database id), or ``None`` when it has none."""
        path = f"{_issue_path(repo, n)}/parent"
        try:
            data = await get_json(self._github, path)
        except GitHubRequestError as exc:
            if exc.status == 404:
                return None
            raise
        return _issue(repo, mapping(data, path))

    async def sub_issues(self, repo: str, n: int) -> list[Issue]:
        """The issue's direct children in GitHub's priority order (every page); a child may
        live in another repo of the same owner."""
        path = f"{_issue_path(repo, n)}/sub_issues"
        return [_issue(repo, item) for item in await get_pages(self._github, path)]

    async def label(self, repo: str, name: str) -> Label | None:
        """One label read live (its name encoded as one path segment); ``None`` when the repo
        has no such label."""
        path = f"/repos/{validate_repo(repo)}/labels/{_label_segment(name)}"
        try:
            data = await get_json(self._github, path)
        except GitHubRequestError as exc:
            if exc.status != 404:
                raise
            return None
        return _label(mapping(data, path))

    async def labels(self, repo: str) -> list[Label]:
        path = f"/repos/{validate_repo(repo)}/labels"
        return [_label(item) for item in await get_pages(self._github, path)]

    async def milestones(self, repo: str) -> list[Milestone]:
        """Open milestones (GitHub's default), every page."""
        path = f"/repos/{validate_repo(repo)}/milestones"
        return [
            Milestone(
                number(item, "number"),
                text(item, "title"),
                text(item, "state"),
                optional_time(item, "due_on"),
            )
            for item in await get_pages(self._github, path)
        ]

    async def comments(
        self, repo: str, n: int, since: datetime | None, page_token: str | None = None
    ) -> Page[IssueComment]:
        """One page of the issue's comments, oldest first, updated at or after ``since``.
        Bodies are untrusted and returned verbatim."""
        path = f"{_issue_path(repo, n)}/comments"
        params = {} if since is None else {"since": github_time(since)}
        items, next_token = await self._page(path, page_token, params)
        return Page([_comment(item) for item in items], next_token)

    async def timeline(
        self, repo: str, n: int, page_token: str | None = None
    ) -> Page[TimelineEvent]:
        """One page of the issue's label, close, reopen and reference events; other events
        (comments among them) are dropped, so a page may be short and still have a next."""
        path = f"{_issue_path(repo, n)}/timeline"
        items, next_token = await self._page(path, page_token)
        events = [_timeline_event(item) for item in items if item.get("event") in TIMELINE_EVENTS]
        return Page(events, next_token)

    async def timeline_links(self, repo: str, n: int) -> list[LinkedPr]:
        """Pull requests that reference the issue (every timeline page), each once, in the
        order they first referenced it."""
        path = f"{_issue_path(repo, n)}/timeline"
        links: dict[tuple[str, int], LinkedPr] = {}
        for item in await get_pages(self._github, path):
            if item.get("event") != "cross-referenced":
                continue
            link = _linked_pr(item)
            if link is not None:
                links.setdefault((link.repo, link.number), link)
        return list(links.values())

    async def file(self, repo: str, path: str) -> str | None:
        """A text file on the default branch (contents API, no ``ref``), or ``None`` when it
        doesn't exist. Used for ``.jesinia/labels.yml``."""
        api_path = (
            f"/repos/{validate_repo(repo)}/contents/"
            f"{'/'.join(path_segment(part) for part in validate_content_path(path).split('/'))}"
        )
        try:
            data = await get_json(self._github, api_path)
        except GitHubRequestError as exc:
            if exc.status == 404:
                return None
            raise
        return _decode_file(path, data)

    # Writes. Owner only: the routes refuse session principals before calling these.

    async def add_labels(self, repo: str, n: int, names: Sequence[str]) -> tuple[str, ...]:
        """Add labels in one write and return every label now on the issue.

        GitHub creates any name the repo lacks: check ``names`` against ``labels(repo)``
        first, since no label may be created implicitly.
        """
        path = f"{_issue_path(repo, n)}/labels"
        data = await self._github.write("POST", path, {"labels": list(validate_label_names(names))})
        return tuple(text(mapping(label, path), "name") for label in list_of(data, path))

    async def remove_label(self, repo: str, n: int, name: str) -> tuple[str, ...]:
        """Remove one label and return the labels left. A label that isn't on the issue or
        pull request (a 404 while it exists) is a no-op; a missing one raises GitHub's 404."""
        path = f"{_issue_path(repo, n)}/labels/{_label_segment(name)}"
        try:
            data = await self._github.write("DELETE", path)
        except GitHubRequestError as exc:
            if exc.status != 404:
                raise
            # Raises the issue's own 404 when the issue is what's missing. Read through the
            # issues endpoint without ``get``'s refusal: label moves cover pull requests too.
            path = _issue_path(repo, n)
            return _label_names(mapping(await get_json(self._github, path), path))
        return tuple(text(mapping(label, path), "name") for label in list_of(data, path))

    async def add_sub_issue(
        self, repo: str, n: int, child_id: int, *, replace_parent: bool = False
    ) -> None:
        """Make the issue with database id ``child_id`` a sub-issue of ``repo#n`` (GitHub
        requires the same owner). A child that already has a parent is refused by GitHub
        unless ``replace_parent`` moves it here."""
        path = f"{_issue_path(repo, n)}/sub_issues"
        payload = {"sub_issue_id": validate_database_id(child_id), "replace_parent": replace_parent}
        await self._github.write("POST", path, payload)

    async def remove_sub_issue(self, repo: str, n: int, child_id: int) -> None:
        path = f"{_issue_path(repo, n)}/sub_issue"
        await self._github.write("DELETE", path, {"sub_issue_id": validate_database_id(child_id)})

    async def reprioritize_sub_issue(
        self, repo: str, n: int, child_id: int, after_id: int | None
    ) -> None:
        """Move a sub-issue to just after ``after_id``, or to the top when ``None``.

        GitHub's API places a child after or before a sibling, so the top means "before
        the current first child", read fresh; a child already first is left alone.
        """
        path = f"{_issue_path(repo, n)}/sub_issues/priority"
        child = validate_database_id(child_id)
        if after_id is not None:
            payload = {"sub_issue_id": child, "after_id": validate_database_id(after_id)}
            await self._github.write("PATCH", path, payload)
            return
        children = await self.sub_issues(repo, n)
        if not children or children[0].id == child:
            return
        await self._github.write(
            "PATCH", path, {"sub_issue_id": child, "before_id": children[0].id}
        )

    async def close(
        self,
        repo: str,
        n: int,
        state_reason: CloseReason,
        duplicate_of: int | None = None,
    ) -> Issue:
        """Close the issue with a reason. ``duplicate_of`` is the canonical issue's database
        id, sent as ``duplicate_issue_id`` and only with ``state_reason="duplicate"``."""
        path = _issue_path(repo, n)
        validate_close(state_reason, duplicate_of)
        payload: dict[str, object] = {"state": "closed", "state_reason": state_reason}
        if duplicate_of is not None:
            payload["duplicate_issue_id"] = duplicate_of
        return _issue(repo, mapping(await self._github.write("PATCH", path, payload), path))

    async def comment(self, repo: str, n: int, body: str) -> CommentRef:
        path = f"{_issue_path(repo, n)}/comments"
        payload = {"body": validate_comment_body(body)}
        return _comment_ref(await self._github.write("POST", path, payload))

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> CommentRef:
        """Edit a comment in place (the same id and URL), never posting a new one."""
        path = f"/repos/{validate_repo(repo)}/issues/comments/{validate_database_id(comment_id)}"
        payload = {"body": validate_comment_body(body)}
        return _comment_ref(await self._github.write("PATCH", path, payload))

    async def create_issue(self, repo: str, title: str, body: str, labels: Sequence[str]) -> Issue:
        """Open an issue. GitHub creates any label in ``labels`` the repo lacks: check them
        against ``labels(repo)`` first, since no label may be created implicitly."""
        path = f"/repos/{validate_repo(repo)}/issues"
        payload: dict[str, object] = {
            "title": validate_issue_title(title),
            "body": validate_issue_body(body),
        }
        if labels:
            payload["labels"] = list(validate_label_names(labels))
        return _issue(repo, mapping(await self._github.write("POST", path, payload), path))

    async def create_label(self, repo: str, name: str, color: str, description: str) -> Label:
        """Create a label: the only way Jesinia creates one, through an approved step."""
        path = f"/repos/{validate_repo(repo)}/labels"
        payload = {
            "name": validate_label_name(name),
            "color": validate_label_color(color),
            "description": validate_label_description(description),
        }
        return _label(mapping(await self._github.write("POST", path, payload), path))

    async def update_label(
        self,
        repo: str,
        name: str,
        *,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Rename or restyle a label; issues keep it under the new name."""
        path = f"/repos/{validate_repo(repo)}/labels/{_label_segment(name)}"
        payload: dict[str, object] = {}
        if new_name is not None:
            payload["new_name"] = validate_label_name(new_name)
        if color is not None:
            payload["color"] = validate_label_color(color)
        if description is not None:
            payload["description"] = validate_label_description(description)
        if not payload:
            raise InvalidLabelError("a label update needs a new name, color or description")
        return _label(mapping(await self._github.write("PATCH", path, payload), path))

    async def delete_label(self, repo: str, name: str) -> None:
        path = f"/repos/{validate_repo(repo)}/labels/{_label_segment(name)}"
        await self._github.write("DELETE", path)
