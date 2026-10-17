# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue's values (spec §2): PR references, cached rows and what a row shows.

Pure: no I/O. Repos are keyed by ``canonical_repo`` (lowercase ``owner/name``) everywhere;
GitHub's own spelling is kept only as ``display_repo`` for the UI.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from jesinia.apps.review.domain.gates import CiState, Depth, GateState, propose_depth
from jesinia.apps.review.domain.workspace import InvalidRepoError, canonical_repo
from jesinia.services.github.domain.pr import PullRequest, ReviewSummary

PreReviewMode = Literal["triage", "everything", "on_open"]
PREREVIEW_MODES: tuple[PreReviewMode, ...] = ("triage", "everything", "on_open")
DEFAULT_PREREVIEW: PreReviewMode = "triage"
AuthorKind = Literal["you", "agent", "developer"]
PrKind = Literal["new", "rereview", "rebase", "stack"]
AutoReviewState = Literal["not_configured", "pending", "landed"]
# A gate's state while it holds the PR back; then queued, running, and ready (done) or
# failed. ``not_started``: the pre-review setting is "on open" and the PR wasn't loaded.
PreReviewState = GateState | Literal["queued", "running", "ready", "failed", "not_started"]

_BOT_SUFFIX = "[bot]"
_NUMBER = r"([1-9][0-9]{0,8})"
_REPO_PART = r"([^/\s#?]+/[^/\s#?]+)"
_TAIL = r"(?:[/?#]\S*)?"
_GITHUB_URL = re.compile(rf"(?:https?://)?(?:www\.)?github\.com/{_REPO_PART}/pull/{_NUMBER}{_TAIL}")
_SHORTHAND = re.compile(rf"{_REPO_PART}#{_NUMBER}")
_ROUTE = re.compile(rf"(?:https?://[^/\s]+)?/?review/{_REPO_PART}/{_NUMBER}{_TAIL}")


class QueueDomainError(Exception):
    """Base for invalid queue values and refused queue actions."""


class InvalidPrRefError(QueueDomainError):
    """Text that names no pull request: not a PR URL, ``owner/repo#n`` or review route."""


class InvalidSettingError(QueueDomainError):
    """A review setting value outside its allowed set."""


class PrNotInQueueError(QueueDomainError):
    """The PR isn't in the queue (never synced or loaded, or already gone)."""


class PrNotOpenError(QueueDomainError):
    """Only open PRs can be loaded for review."""


def _canonical(repo: str) -> str:
    try:
        return canonical_repo(repo)
    except InvalidRepoError as exc:
        raise InvalidPrRefError(str(exc)) from exc


@dataclass(frozen=True)
class PrRef:
    """A PR's queue key: canonical repo plus number."""

    repo: str
    number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", _canonical(self.repo))
        if self.number < 1:
            raise InvalidPrRefError(f"PR numbers start at 1, got {self.number}")

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


def parse_pr_ref(ref: str) -> PrRef:
    """Read a GitHub PR URL, ``owner/repo#n`` or a ``/review/<owner>/<repo>/<n>`` route
    (bare, or as a full Jesinia URL).

    Raises:
        InvalidPrRefError: anything else, including an invalid owner or repo name.
    """
    text = ref.strip()
    for pattern in (_GITHUB_URL, _SHORTHAND, _ROUTE):
        if match := pattern.fullmatch(text):
            return PrRef(match.group(1), int(match.group(2)))
    raise InvalidPrRefError(f"not a PR URL, owner/repo#n or review link: {ref!r}")


def parse_prereview_mode(value: str) -> PreReviewMode:
    """Raises: InvalidSettingError: not one of ``PREREVIEW_MODES``."""
    for mode in PREREVIEW_MODES:
        if value == mode:
            return mode
    raise InvalidSettingError(f"pre-review must be one of {', '.join(PREREVIEW_MODES)}")


@dataclass(frozen=True)
class RepoConfig:
    """A repo's review settings; ``prereview`` overrides the user's default when set."""

    repo: str
    watched: bool = False
    automated_reviewer: str | None = None
    prereview: PreReviewMode | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", _canonical(self.repo))
        if self.prereview is not None:
            parse_prereview_mode(self.prereview)


@dataclass(frozen=True)
class PrSnapshot:
    """What GitHub said about a PR on the last good fetch."""

    ref: PrRef
    display_repo: str
    title: str
    author: str
    author_is_bot: bool
    url: str
    head_sha: str
    base_sha: str
    base_ref: str
    head_ref: str
    head_repo: str | None
    created_at: datetime
    size_lines: int
    files: int
    review_requested: bool
    ci_state: CiState
    auto_review_state: AutoReviewState
    review_comment_count: int
    bot_review_count: int
    human_review_count: int
    depth: Depth


@dataclass(frozen=True)
class StoredPr:
    """A queue row as ``review.sqlite`` holds it: the snapshot plus local state."""

    pr: PrSnapshot
    kind: PrKind
    state: PreReviewState
    state_reason: str
    last_head_change_at: datetime
    loaded: bool
    dismissed: bool
    fetch_error: str | None
    fetch_error_at: datetime | None


@dataclass(frozen=True)
class RoundStatus:
    """Whether a pre-review round on one head has finished, and why it failed if it did."""

    finished: bool
    error: str | None


@dataclass(frozen=True)
class EvidenceSummary:
    """The recommendation's evidence in one line: CI, tests, finding counts by group."""

    ci: str
    tests: str | None
    findings: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundSummary:
    """The latest finished round's outcome, as the queue row shows it."""

    head_sha: str
    verdict: str | None
    evidence: EvidenceSummary | None
    coverage_pct: float | None


def evidence_from_json(data: object) -> EvidenceSummary | None:
    """The ``evidence`` artifact (``{ci, tests, findings: {group: n}}``), or ``None`` if it
    has another shape."""
    if not isinstance(data, Mapping):
        return None
    ci, tests, findings = data.get("ci"), data.get("tests"), data.get("findings", {})
    if not isinstance(ci, str) or not (tests is None or isinstance(tests, str)):
        return None
    if not isinstance(findings, Mapping):
        return None
    counts: dict[str, int] = {}
    for group, count in findings.items():
        if not isinstance(group, str) or not isinstance(count, int) or isinstance(count, bool):
            return None
        counts[group] = count
    return EvidenceSummary(ci, tests, counts)


def coverage_from_json(data: object) -> float | None:
    """The ``coverage`` artifact's ``pct`` (0 to 100), or ``None`` if it has another shape."""
    pct = data.get("pct") if isinstance(data, Mapping) else None
    if isinstance(pct, bool) or not isinstance(pct, int | float) or not 0 <= pct <= 100:
        return None
    return float(pct)


@dataclass(frozen=True)
class QueueRow:
    """Everything a queue row shows (spec §2 "Each row shows")."""

    repo: str
    display_repo: str
    number: int
    title: str
    url: str
    author: str
    author_kind: AuthorKind
    created_at: datetime
    size_lines: int
    files: int
    kind: PrKind
    depth: Depth
    ci_state: CiState
    auto_review_state: AutoReviewState
    review_comment_count: int
    bot_review_count: int
    human_review_count: int
    review_requested: bool
    prereview_state: PreReviewState
    prereview_reason: str
    recommendation: str | None
    recommendation_stale: bool
    evidence: EvidenceSummary | None
    coverage_pct: float | None
    loaded: bool
    dismissed: bool
    head_sha: str
    fetch_error: str | None
    fetch_error_at: datetime | None


@dataclass(frozen=True)
class QueueFilter:
    repo: str | None = None
    include_dismissed: bool = False

    def __post_init__(self) -> None:
        if self.repo is not None:
            object.__setattr__(self, "repo", _canonical(self.repo))


@dataclass(frozen=True)
class QueueView:
    rows: list[QueueRow]
    synced_at: datetime | None
    syncing: bool
    sync_error: str | None


def _login(name: str) -> str:
    return name.lower().removesuffix(_BOT_SUFFIX)


def auto_review_state(
    reviewer: str | None, reviews: Sequence[ReviewSummary], head_sha: str
) -> AutoReviewState:
    """Whether the repo's automated reviewer (matched without case or ``[bot]``) has
    reviewed ``head_sha``."""
    if reviewer is None:
        return "not_configured"
    wanted = _login(reviewer)
    landed = any(_login(r.author) == wanted and r.commit_sha == head_sha for r in reviews)
    return "landed" if landed else "pending"


def snapshot_of(
    pr: PullRequest,
    *,
    ci_state: CiState,
    reviews: Sequence[ReviewSummary],
    automated_reviewer: str | None,
    viewer: str | None,
) -> PrSnapshot:
    """A ``PullRequest`` with its CI and reviews, as the queue caches it; the viewer's own
    reviews don't count as someone else's."""
    lines = pr.additions + pr.deletions
    others = [r for r in reviews if viewer is None or _login(r.author) != _login(viewer)]
    return PrSnapshot(
        ref=PrRef(pr.repo, pr.number),
        display_repo=pr.repo,
        title=pr.title,
        author=pr.author,
        author_is_bot=pr.author_is_bot,
        url=pr.url,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        base_ref=pr.base_ref,
        head_ref=pr.head_ref,
        head_repo=pr.head_repo,
        created_at=pr.created_at,
        size_lines=lines,
        files=pr.changed_files,
        review_requested=pr.review_requested,
        ci_state=ci_state,
        auto_review_state=auto_review_state(automated_reviewer, reviews, pr.head_sha),
        review_comment_count=sum(r.comment_count for r in reviews),
        bot_review_count=sum(1 for r in others if r.author_is_bot),
        human_review_count=sum(1 for r in others if not r.author_is_bot),
        depth=propose_depth(lines, pr.changed_files, None),
    )


def author_kind(author: str, author_is_bot: bool, viewer: str | None) -> AuthorKind:
    if viewer is not None and _login(author) == _login(viewer):
        return "you"
    return "agent" if author_is_bot else "developer"


def queue_priority(loaded: bool, review_requested: bool, author_is_bot: bool) -> int:
    """Lower runs first: loaded, then requested, then agent-authored, then the rest."""
    if loaded:
        return 0
    if review_requested:
        return 1
    return 2 if author_is_bot else 3


def queue_order(stored: StoredPr) -> tuple[int, datetime, str, int]:
    pr = stored.pr
    priority = queue_priority(stored.loaded, pr.review_requested, pr.author_is_bot)
    return priority, pr.created_at, pr.ref.repo, pr.ref.number


def queue_row(stored: StoredPr, latest: RoundSummary | None, viewer: str | None) -> QueueRow:
    """The row the UI shows. A PR whose last fetch failed says so, whatever state it had."""
    pr = stored.pr
    state, reason = stored.state, stored.state_reason
    if stored.fetch_error is not None:
        state, reason = "fetch_failed", f"couldn't load: {stored.fetch_error}"
    return QueueRow(
        repo=pr.ref.repo,
        display_repo=pr.display_repo,
        number=pr.ref.number,
        title=pr.title,
        url=pr.url,
        author=pr.author,
        author_kind=author_kind(pr.author, pr.author_is_bot, viewer),
        created_at=pr.created_at,
        size_lines=pr.size_lines,
        files=pr.files,
        kind=stored.kind,
        depth=pr.depth,
        ci_state=pr.ci_state,
        auto_review_state=pr.auto_review_state,
        review_comment_count=pr.review_comment_count,
        bot_review_count=pr.bot_review_count,
        human_review_count=pr.human_review_count,
        review_requested=pr.review_requested,
        prereview_state=state,
        prereview_reason=reason,
        recommendation=None if latest is None else latest.verdict,
        recommendation_stale=latest is not None and latest.head_sha != pr.head_sha,
        evidence=None if latest is None else latest.evidence,
        coverage_pct=None if latest is None else latest.coverage_pct,
        loaded=stored.loaded,
        dismissed=stored.dismissed,
        head_sha=pr.head_sha,
        fetch_error=stored.fetch_error,
        fetch_error_at=stored.fetch_error_at,
    )
