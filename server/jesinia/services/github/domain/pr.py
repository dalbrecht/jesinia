# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pull requests as the review tool sees them, independent of GitHub's wire format.

Pure: no I/O. The application layer maps GitHub payloads onto these types.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

CheckStatus = Literal["pending", "passing", "failing"]
RollupState = Literal["pending", "passing", "failing", "none"]
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
ReviewSide = Literal["LEFT", "RIGHT"]

_SIDES: frozenset[str] = frozenset({"LEFT", "RIGHT"})
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_PASSING_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})
_BOT_SUFFIX = "[bot]"


class PullRequestDomainError(ValueError):
    """Base for invalid pull request values."""


class InvalidPullRequestReferenceError(PullRequestDomainError):
    """A repo name, PR number or commit SHA that cannot address GitHub safely."""


class InvalidDraftCommentError(PullRequestDomainError):
    """A draft inline comment GitHub would reject."""


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    body: str
    author: str
    author_is_bot: bool
    base_ref: str
    head_ref: str
    head_sha: str
    base_sha: str
    head_repo: str | None  # owner/name of the head branch's repo; None once a fork is deleted
    state: str  # "open", "closed" or "merged"
    draft: bool
    additions: int
    deletions: int
    changed_files: int
    url: str
    review_requested: bool
    mergeable_state: str | None
    created_at: datetime
    # Set when the inbox could list this PR only from its search hit: the PR fetch failed
    # with this cause, so the head, base and size fields are empty.
    fetch_error: str | None = None


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    url: str | None


@dataclass(frozen=True)
class CheckState:
    state: RollupState
    checks: list[Check]


@dataclass(frozen=True)
class ReviewSummary:
    author: str
    author_is_bot: bool
    state: str
    commit_sha: str | None
    submitted_at: datetime | None
    comment_count: int


@dataclass(frozen=True)
class ReviewThread:
    path: str
    line: int | None
    body: str
    author: str
    resolved: bool
    commit_sha: str | None


@dataclass(frozen=True)
class PrFile:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_filename: str | None


@dataclass(frozen=True)
class PrCommit:
    sha: str
    message: str
    author: str
    authored_at: datetime | None


@dataclass(frozen=True)
class DraftComment:
    """An inline review comment; a multi-line range needs both ``start_line`` and
    ``start_side``, because GitHub rejects a range without its starting side."""

    path: str
    line: int
    side: ReviewSide
    body: str
    start_line: int | None = None
    start_side: ReviewSide | None = None

    def __post_init__(self) -> None:
        if not self.path:
            raise InvalidDraftCommentError("a draft comment needs a file path")
        if not self.body.strip():
            raise InvalidDraftCommentError("a draft comment needs a body")
        if self.line < 1:
            raise InvalidDraftCommentError(f"line must be 1 or more, got {self.line}")
        if self.side not in _SIDES:
            raise InvalidDraftCommentError(f"side must be LEFT or RIGHT, got {self.side!r}")
        if self.start_line is None:
            if self.start_side is not None:
                raise InvalidDraftCommentError("start_side was given without start_line")
            return
        if self.start_side is None:
            raise InvalidDraftCommentError("start_line needs start_side")
        if self.start_side not in _SIDES:
            raise InvalidDraftCommentError(
                f"start_side must be LEFT or RIGHT, got {self.start_side!r}"
            )
        if self.start_line >= self.line:
            raise InvalidDraftCommentError(
                f"start_line {self.start_line} must come before line {self.line}"
            )


def is_bot_account(login: str, account_type: str | None) -> bool:
    """GitHub marks apps with ``type == "Bot"``; their logins also end in ``[bot]``."""
    return account_type == "Bot" or login.endswith(_BOT_SUFFIX)


def check_status_from_run(status: str, conclusion: str | None) -> CheckStatus:
    """Map a check run: unfinished is pending; success, neutral and skipped pass."""
    if status != "completed":
        return "pending"
    return "passing" if conclusion in _PASSING_CONCLUSIONS else "failing"


def check_status_from_commit_status(state: str) -> CheckStatus:
    """Map a legacy commit status (``success``, ``pending``, ``failure`` or ``error``)."""
    if state == "success":
        return "passing"
    return "pending" if state == "pending" else "failing"


def rollup_checks(checks: Sequence[Check]) -> CheckState:
    """Any failing check fails the head; otherwise any pending one keeps it pending."""
    statuses = {check.status for check in checks}
    state: RollupState
    if not statuses:
        state = "none"
    elif "failing" in statuses:
        state = "failing"
    elif "pending" in statuses:
        state = "pending"
    else:
        state = "passing"
    return CheckState(state, list(checks))


def validate_repo(repo: str) -> str:
    """Return ``repo`` if it is a plain ``owner/name``, so it can't steer a request path."""
    name = repo.partition("/")[2]
    if not _REPO.fullmatch(repo) or name in {".", ".."}:
        raise InvalidPullRequestReferenceError(f"repo must be owner/name, got {repo!r}")
    return repo


def validate_pull_number(number: int) -> int:
    if number < 1:
        raise InvalidPullRequestReferenceError(f"PR number must be 1 or more, got {number}")
    return number


def validate_sha(sha: str) -> str:
    if not _SHA.fullmatch(sha):
        raise InvalidPullRequestReferenceError(f"commit sha must be hex, got {sha!r}")
    return sha
