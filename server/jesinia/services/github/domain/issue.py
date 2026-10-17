# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub issues as the Issues app sees them, independent of GitHub's wire format.

Pure: no I/O. The application layer maps GitHub payloads onto these types and checks every
caller-supplied value here before it can reach a request path or body. Issue and comment
text is untrusted and returned verbatim; it is fenced by its consumers, never here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, get_args

IssueState = Literal["open", "closed"]
CloseReason = Literal["completed", "not_planned", "duplicate"]

# GitHub's documented limits: comment and issue bodies, titles, label names and descriptions.
COMMENT_BODY_LIMIT = 65_536
TITLE_LIMIT = 256
LABEL_NAME_LIMIT = 50
LABEL_DESCRIPTION_LIMIT = 100
# GitHub refuses more than 100 labels on one issue.
LABELS_PER_ISSUE_LIMIT = 100

_CLOSE_REASONS: frozenset[str] = frozenset(get_args(CloseReason))
_COLOR = re.compile(r"[0-9A-Fa-f]{6}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PAGE = re.compile(r"[1-9][0-9]*")
# Linked PR states, most decisive first: a merged PR settles an issue, an open one is work
# in flight, a closed one is history.
_LINKED_PR_ORDER = ("merged", "open", "closed")


class IssueDomainError(ValueError):
    """Base for invalid issue values."""


class InvalidIssueReferenceError(IssueDomainError):
    """An issue number, database id, page token, time or path that cannot address GitHub
    safely."""


class InvalidLabelError(IssueDomainError):
    """A label name, colour or description GitHub would reject."""


class InvalidIssueTextError(IssueDomainError):
    """An issue title, issue body or comment body outside GitHub's limits."""


class InvalidCloseError(IssueDomainError):
    """A close request GitHub would reject or misread."""


@dataclass(frozen=True)
class Page[T]:
    """One page of a GitHub list; pass ``next_token`` back for the next, ``None`` at the end."""

    items: list[T]
    next_token: str | None


@dataclass(frozen=True)
class IssueRef:
    """Another issue; ``id`` is GitHub's database id when the payload carried it."""

    repo: str
    number: int
    id: int | None = None


@dataclass(frozen=True)
class Issue:
    """One issue.

    ``parent`` comes from the payload's ``parent_issue_url`` when GitHub includes it (no id
    then). ``sub_issues`` stays empty from list and get reads: GitHub sends only the counts
    (``sub_issue_count``), and the children are a separate ``IssueClient.sub_issues`` call
    that sync makes selectively. ``linked_pr_state`` likewise stays ``None`` until a caller
    fills it from ``IssueClient.timeline_links`` (see ``summarize_linked_prs``).
    """

    repo: str
    number: int
    id: int  # GitHub's database id: sub-issue links and ``duplicate_issue_id`` need it
    title: str
    body: str
    state: IssueState
    state_reason: str | None
    labels: tuple[str, ...]
    milestone: str | None
    parent: IssueRef | None
    sub_issues: tuple[IssueRef, ...]
    created_at: datetime
    updated_at: datetime
    url: str
    author: str
    linked_pr_state: str | None = None
    sub_issue_count: int = 0


@dataclass(frozen=True)
class LabeledItem:
    """An issue or pull request carrying a label, as GitHub's label filter lists it now."""

    number: int
    labels: tuple[str, ...]
    is_pull_request: bool


@dataclass(frozen=True)
class Label:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Milestone:
    number: int
    title: str
    state: str
    due_on: datetime | None


@dataclass(frozen=True)
class IssueComment:
    id: int
    author: str
    body: str
    created_at: datetime
    url: str


@dataclass(frozen=True)
class CommentRef:
    id: int
    url: str


@dataclass(frozen=True)
class TimelineEvent:
    """A label, close, reopen or reference event. ``source`` is the referencing issue or PR
    of a ``cross-referenced`` event; ``commit_id`` the commit of a ``referenced`` or
    ``closed`` one. Cross-references carry no id."""

    id: int | None
    event: str
    actor: str
    created_at: datetime | None
    label: str | None = None
    state_reason: str | None = None
    source: IssueRef | None = None
    commit_id: str | None = None


@dataclass(frozen=True)
class LinkedPr:
    repo: str
    number: int
    state: str  # "open", "closed" or "merged"
    url: str


def summarize_linked_prs(links: Sequence[LinkedPr]) -> str | None:
    """The one state that best describes an issue's linked PRs: merged, else open, else
    closed; ``None`` without any."""
    states = {link.state for link in links}
    return next((state for state in _LINKED_PR_ORDER if state in states), None)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def validate_issue_number(number: int) -> int:
    if not _is_positive_int(number):
        raise InvalidIssueReferenceError(f"issue number must be 1 or more, got {number!r}")
    return number


def validate_database_id(database_id: int) -> int:
    if not _is_positive_int(database_id):
        raise InvalidIssueReferenceError(f"database id must be 1 or more, got {database_id!r}")
    return database_id


def validate_page_token(token: str | None) -> int:
    """The page number a token names; ``None`` is the first page."""
    if token is None:
        return 1
    if not _PAGE.fullmatch(token):
        raise InvalidIssueReferenceError(f"page token must be a page number, got {token!r}")
    return int(token)


def github_time(moment: datetime) -> str:
    """``moment`` as GitHub's ISO 8601 UTC form; a naive time is refused, since its zone is
    a guess."""
    if moment.tzinfo is None:
        raise InvalidIssueReferenceError("a time sent to GitHub needs a timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_label_name(name: str) -> str:
    if not name.strip() or name != name.strip():
        raise InvalidLabelError(f"label name must be non-blank and unpadded, got {name!r}")
    if len(name) > LABEL_NAME_LIMIT:
        raise InvalidLabelError(f"label name is over {LABEL_NAME_LIMIT} characters: {name!r}")
    if _CONTROL.search(name):
        raise InvalidLabelError(f"label name has a control character: {name!r}")
    if not name.strip("."):
        # ``.`` and ``..`` would be dot segments in a label's URL; refuse every dot-only name.
        raise InvalidLabelError(f"label name can't be only dots: {name!r}")
    return name


def validate_label_filter(name: str) -> str:
    """A label name GitHub's ``labels=`` filter can express: it is a comma-separated list, so
    a name with a comma would match other labels (or none)."""
    validate_label_name(name)
    if "," in name:
        raise InvalidLabelError(f"GitHub can't list issues by a label name with a comma: {name!r}")
    return name


def validate_label_names(names: Sequence[str]) -> tuple[str, ...]:
    """Each name checked, repeats dropped, order kept; at least one, at most GitHub's cap."""
    unique = tuple(dict.fromkeys(validate_label_name(name) for name in names))
    if not unique:
        raise InvalidLabelError("at least one label name is needed")
    if len(unique) > LABELS_PER_ISSUE_LIMIT:
        raise InvalidLabelError(f"at most {LABELS_PER_ISSUE_LIMIT} labels fit on one issue")
    return unique


def validate_label_color(color: str) -> str:
    if not _COLOR.fullmatch(color):
        raise InvalidLabelError(f"label color must be six hex digits without '#', got {color!r}")
    return color


def validate_label_description(description: str) -> str:
    if len(description) > LABEL_DESCRIPTION_LIMIT:
        raise InvalidLabelError(f"label description is over {LABEL_DESCRIPTION_LIMIT} characters")
    return description


def validate_comment_body(body: str) -> str:
    if not body.strip():
        raise InvalidIssueTextError("a comment needs a body")
    return validate_issue_body(body)


def validate_issue_body(body: str) -> str:
    if len(body) > COMMENT_BODY_LIMIT:
        raise InvalidIssueTextError(
            f"body is {len(body)} characters; GitHub's limit is {COMMENT_BODY_LIMIT}"
        )
    return body


def validate_issue_title(title: str) -> str:
    if not title.strip():
        raise InvalidIssueTextError("an issue needs a title")
    if len(title) > TITLE_LIMIT:
        raise InvalidIssueTextError(f"title is over {TITLE_LIMIT} characters")
    return title


def validate_close(state_reason: CloseReason, duplicate_of: int | None) -> None:
    """``duplicate_of`` (the canonical issue's database id) only goes with ``duplicate``."""
    if state_reason not in _CLOSE_REASONS:
        raise InvalidCloseError(f"unknown close reason {state_reason!r}")
    if duplicate_of is None:
        return
    if state_reason != "duplicate":
        raise InvalidCloseError("duplicate_of needs state_reason 'duplicate'")
    validate_database_id(duplicate_of)


def validate_content_path(path: str) -> str:
    """A repo-relative file path: no absolute path, no ``.``/``..`` or empty segment, no
    backslash or control character, so it can't leave the contents API's tree."""
    segments = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or _CONTROL.search(path)
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise InvalidIssueReferenceError(f"path must be a plain repo-relative path, got {path!r}")
    return path
