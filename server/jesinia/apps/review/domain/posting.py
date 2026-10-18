# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drafts and posting (spec §7): what a review will post, and the rules before it may.

Pure: no I/O. A review is one atomic GitHub review against the round's head: a body plus
inline comments. A draft GitHub can't anchor (outside its diff, or marked as a general
note) goes into the body instead, so the review still posts. Approving while any included
text says ``BLOCKING`` (any case), posting while the PR has newer commits, and anything but
a comment on the author's own PR are warnings the author must see first.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from jesinia.apps.review.domain.errors import ReviewError
from jesinia.services.github.domain.pr import (
    DraftComment,
    InvalidDraftCommentError,
    PrFile,
    ReviewEvent,
    ReviewSide,
)

PostingState = Literal["started", "uncertain", "posted", "not_posted", "failed"]
# Attempts whose outcome isn't known yet: they block new posts until reconciled.
UNSETTLED: Final[frozenset[str]] = frozenset({"started", "uncertain"})
PostWarning = Literal[
    "blocking_comment", "approve_against_evidence", "newer_head", "own_pr_comment_only"
]
POST_WARNINGS: Final[tuple[PostWarning, ...]] = (
    "blocking_comment",
    "approve_against_evidence",
    "newer_head",
    "own_pr_comment_only",
)
REVIEW_EVENTS: Final[tuple[ReviewEvent, ...]] = ("APPROVE", "REQUEST_CHANGES", "COMMENT")
# GitHub's limit for a comment or review body is 65,536 characters.
BODY_MAX: Final = 65_000
PATH_MAX: Final = 1024
DRAFTS_MAX: Final = 500
MARKER_NOTE: Final = (
    "Jesinia adds an invisible marker (an HTML comment) to the end of the body, so a retry "
    "after a lost response finds this review instead of posting it twice"
)

_BLOCKING = re.compile("blocking", re.IGNORECASE)
# The marker's name: no text that reaches the review body may carry it, in any spelling
# (ruling R48), or it could make a review look like another attempt's.
_MARKER_NAME: Final = "jesinia-posting"
_SPACE = re.compile(r"\s+")
# Review states that are submitted (a PENDING review is invisible to everyone else).
SUBMITTED_STATES: Final = frozenset({"APPROVED", "COMMENTED", "CHANGES_REQUESTED"})
# Placeholders for agent text that carried the marker's name: the finding stays visible.
WITHHELD_TEXT: Final = (
    "[Jesinia removed this finding's text: it contained Jesinia's posting marker]"
)
WITHHELD_PATH: Final = "(path withheld: it contained Jesinia's posting marker)"
_HUNK = re.compile(r"^@@ -(\d{1,10})(?:,(\d{1,10}))? \+(\d{1,10})(?:,(\d{1,10}))? @@", re.M)
_SEVERITY_TITLES: Final = {"critical": "Critical", "important": "Important", "minor": "Minor"}
_GROUPS: Final = frozenset({"correctness", "design", "style"})


class PostingDomainError(ReviewError):
    """Base for drafts and posts Jesinia refuses."""


class InvalidDraftError(PostingDomainError):
    """A draft GitHub would reject (or Jesinia couldn't post whole)."""


class NoReviewRound(PostingDomainError):  # noqa: N818 - the error code clients see
    """The PR (or round) has no review round to draft or post from."""


class NeedsConfirmation(PostingDomainError):  # noqa: N818 - the plan's name
    """The author hasn't confirmed every warning the preview showed; ``warnings`` lists
    them all."""

    def __init__(self, warnings: Sequence[PostWarning]) -> None:
        super().__init__(f"confirm before posting: {', '.join(warnings)}")
        self.warnings = tuple(warnings)


class ReviewedHeadGone(PostingDomainError):  # noqa: N818 - the plan's name
    """The commit the round reviewed is no longer part of the PR (it was force-pushed
    away): review the new head instead."""


class CommentOnlyOnOwnPr(PostingDomainError):  # noqa: N818 - the error code clients see
    """GitHub doesn't let authors approve or request changes on their own PR."""


class PreviewOutdated(PostingDomainError):  # noqa: N818 - the error code clients see
    """What would be posted changed since the preview the author confirmed."""


class RoundChanged(PostingDomainError):  # noqa: N818 - the error code clients see
    """The round a save, preview or post names is no longer the PR's current round: it is
    refused, never moved onto the new round (the author's text stays with them)."""


class EarlierReviewPosted(PostingDomainError):  # noqa: N818 - the error code clients see
    """An earlier attempt whose response was lost turned out to be on GitHub, and it isn't
    the review now being posted (another digest or event): that earlier version is what
    GitHub has, and the newer edits were not sent (ruling R47). ``url`` is its page and
    ``event`` its event."""

    def __init__(self, url: str, event: ReviewEvent) -> None:
        super().__init__(
            f"an earlier version of this review ({event}) is already on GitHub at {url}; "
            "your newer edits were not sent"
        )
        self.url = url
        self.event = event


class InvalidDraftContent(PostingDomainError):  # noqa: N818 - the error code clients see
    """A draft or the summary carries Jesinia's posting marker text (in any spelling); it
    would let a review pass for another attempt's, so it is refused, never stripped.
    ``field`` says which (``summary`` or ``draft``); ``draft_index`` is the draft's (0-based)."""

    def __init__(self, message: str, *, field: str = "draft", draft_index: int | None = None):
        super().__init__(message)
        self.field = field
        self.draft_index = draft_index


class PostingInProgress(PostingDomainError):  # noqa: N818 - the error code clients see
    """Another attempt to post this round's review is in flight (another process won):
    retry after ``retry_after`` seconds."""

    retry_after: int = 5


class PostingStillUncertain(PostingDomainError):  # noqa: N818 - the error code clients see
    """An earlier attempt may still land on GitHub, and isn't listed yet: retry after
    ``retry_after`` seconds; nothing was sent."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "an earlier attempt to post this review may still appear on GitHub; nothing was "
            f"sent: retry in {retry_after} s"
        )
        self.retry_after = retry_after


class EmptyReview(PostingDomainError):  # noqa: N818 - the error code clients see
    """GitHub needs a body to request changes or comment."""


class ReviewBodyTooLong(PostingDomainError):  # noqa: N818 - the error code clients see
    """The composed review body (summary, outside-diff drafts with their labels, and the
    posting marker) is over GitHub's limit; GitHub would refuse the review."""

    def __init__(self, length: int, limit: int) -> None:
        super().__init__(
            f"the review body is {length:,} characters; GitHub allows {limit:,}: shorten "
            "the drafts or move some inline"
        )
        self.length = length
        self.limit = limit


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidDraftError(message)


@dataclass(frozen=True)
class Draft:
    """A draft review comment. Inline drafts are anchored like GitHub's comments: ``line``
    and ``side``, plus ``start_line`` and ``start_side`` for a range. ``outside_diff``
    drafts post in the review body; their lines, if any, only label them there.

    Raises:
        InvalidDraftError: an empty path or body, a body over ``BODY_MAX``, an inline draft
            without a line and side, or a range GitHub would reject.
    """

    path: str
    body: str
    line: int | None = None
    side: ReviewSide | None = None
    start_line: int | None = None
    start_side: ReviewSide | None = None
    origin_finding: str | None = None
    outdated: bool = False
    outside_diff: bool = False
    id: int | None = None

    def __post_init__(self) -> None:
        _check(0 < len(self.path) <= PATH_MAX, "a draft needs a file path")
        _check(bool(self.body.strip()), "a draft needs a body")
        _check(len(self.body) <= BODY_MAX, f"a draft is at most {BODY_MAX} characters")
        if self.outside_diff:
            _check(self.line is None or self.line >= 1, "line must be 1 or more")
            _check(
                self.start_line is None or self.line is not None and self.start_line < self.line,
                "start_line must come before line",
            )
            return
        _check(self.line is not None and self.side is not None, "an inline draft needs a line")
        self.comment()

    def comment(self) -> DraftComment:
        """The draft as GitHub's inline comment.

        Raises:
            InvalidDraftError: it can't be one (an outside-diff draft, a bad range).
        """
        if self.outside_diff or self.line is None or self.side is None:
            raise InvalidDraftError("an outside-the-diff draft posts in the body")
        try:
            return DraftComment(
                self.path, self.line, self.side, self.body, self.start_line, self.start_side
            )
        except InvalidDraftCommentError as exc:
            raise InvalidDraftError(str(exc)) from exc


@dataclass(frozen=True)
class Posting:
    """One attempt to post a round's review, as recorded before GitHub was called."""

    id: int
    round_id: int
    event: ReviewEvent
    key: str
    digest: str
    body: str
    started_at: datetime
    state: PostingState
    review_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_end: int
    new_start: int
    new_end: int

    def holds(self, line: int, side: ReviewSide) -> bool:
        if side == "LEFT":
            return self.old_start <= line <= self.old_end
        return self.new_start <= line <= self.new_end


@dataclass(frozen=True)
class CommentableLines:
    """Where GitHub takes inline comments: the hunks of each file's patch in its own diff
    of the PR at the reviewed head."""

    hunks: Mapping[str, tuple[_Hunk, ...]] = field(default_factory=dict)

    def anchors(self, draft: Draft) -> bool:
        """Whether GitHub can anchor ``draft`` inline: its line (and its range's start)
        inside one hunk, on their sides."""
        if draft.outside_diff or draft.line is None or draft.side is None:
            return False
        start, start_side = draft.start_line or draft.line, draft.start_side or draft.side
        return any(
            hunk.holds(draft.line, draft.side) and hunk.holds(start, start_side)
            for hunk in self.hunks.get(draft.path, ())
        )


def commentable_lines(files: Sequence[PrFile]) -> CommentableLines:
    """The commentable lines of GitHub's file list; a file without a patch (binary, or too
    large for GitHub to show) takes no inline comment."""
    hunks: dict[str, tuple[_Hunk, ...]] = {}
    for item in files:
        if item.patch is None:
            continue
        found: list[_Hunk] = []
        for match in _HUNK.finditer(item.patch):
            old_start, new_start = int(match.group(1)), int(match.group(3))
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            found.append(
                _Hunk(old_start, old_start + old_count - 1, new_start, new_start + new_count - 1)
            )
        hunks[item.filename] = tuple(found)
    return CommentableLines(hunks)


@dataclass(frozen=True)
class Review:
    """What one GitHub review sends: its body and inline comments."""

    body: str
    comments: tuple[DraftComment, ...]


def _code(path: str) -> str:
    """``path`` as inline code, with a fence longer than any backtick run in it."""
    run = max((len(m) for m in re.findall(r"`+", path)), default=0)
    fence = "`" * (run + 1)
    return f"{fence} {path} {fence}"


def _label(draft: Draft) -> str:
    if draft.line is None:
        return _code(draft.path)
    if draft.start_line is not None:
        return f"{_code(draft.path)}, lines {draft.start_line}–{draft.line}"
    return f"{_code(draft.path)}, line {draft.line}"


def carries_marker_text(text: str) -> bool:
    """Whether ``text`` names the posting marker in any spelling: after decoding HTML
    entities (until nothing changes, so nested entities can't hide it), removing
    whitespace and ignoring case."""
    decoded, previous = text, None
    while decoded != previous:
        previous, decoded = decoded, html.unescape(decoded)
    return _MARKER_NAME in _SPACE.sub("", decoded).casefold()


def check_review_text(summary: str, drafts: Sequence[Draft]) -> None:
    """Refuse marker text in anything that can reach the review: the summary, and each
    draft's body and path (the path labels it in the body).

    Raises:
        InvalidDraftContent: naming the summary or the draft (1-based).
    """
    if carries_marker_text(summary):
        raise InvalidDraftContent(
            "the summary contains Jesinia's posting marker text", field="summary"
        )
    for number, draft in enumerate(drafts, start=1):
        if carries_marker_text(draft.body) or carries_marker_text(draft.path):
            raise InvalidDraftContent(
                f"draft {number} contains Jesinia's posting marker text; remove it",
                draft_index=number - 1,
            )


def compose_review(summary: str, drafts: Sequence[Draft], commentable: CommentableLines) -> Review:
    """The review the drafts make: anchored drafts inline, in order; every other draft (an
    outside-the-diff note, or one GitHub can't anchor) in the body after the summary, under
    its file and lines.

    Raises:
        InvalidDraftContent: marker text in the summary or a draft (R48).
    """
    check_review_text(summary, drafts)
    inline = [draft.comment() for draft in drafts if commentable.anchors(draft)]
    in_body = [draft for draft in drafts if not commentable.anchors(draft)]
    parts = [summary.strip()] if summary.strip() else []
    if in_body:
        notes = "\n\n".join(f"**{_label(draft)}**\n\n{draft.body.strip()}" for draft in in_body)
        parts.append(f"#### Comments outside the diff\n\n{notes}")
    return Review("\n\n".join(parts), tuple(inline))


def check_body_length(review: Review) -> None:
    """Refuse a review whose body, once the posting marker is added, is over GitHub's limit.
    Inline comments need no check: each is one draft, at most ``BODY_MAX`` characters.

    Raises:
        ReviewBodyTooLong: with the length as posted (the marker's largest size counted).
    """
    length = len(review.body) + MARKER_RESERVE
    if length > GITHUB_BODY_LIMIT:
        raise ReviewBodyTooLong(length, GITHUB_BODY_LIMIT)


def has_blocking(body: str, comments: Sequence[DraftComment]) -> bool:
    """Whether the body or any comment says ``blocking``, in any case."""
    return any(_BLOCKING.search(text) for text in (body, *(c.body for c in comments)))


def review_warnings(
    event: ReviewEvent,
    review: Review,
    *,
    own_pr: bool,
    newer_head: bool,
    against_evidence: bool = False,
) -> tuple[PostWarning, ...]:
    """The warnings the confirmation shows, in a fixed order. ``against_evidence``: the
    round is unfinished or Jesinia's verdict isn't approve, so approving needs the author's
    explicit confirmation (R54: Jesinia recommends, the author decides)."""
    warnings: list[PostWarning] = []
    if event == "APPROVE" and has_blocking(review.body, review.comments):
        warnings.append("blocking_comment")
    if event == "APPROVE" and against_evidence:
        warnings.append("approve_against_evidence")
    if newer_head:
        warnings.append("newer_head")
    if own_pr and event != "COMMENT":
        warnings.append("own_pr_comment_only")
    return tuple(warnings)


def review_digest(round_id: int, head_sha: str, event: ReviewEvent, review: Review) -> str:
    """A fingerprint of exactly what would be posted, where and how: the post must match
    the preview the author confirmed."""
    payload = {
        "round": round_id,
        "head": head_sha,
        "event": event,
        "body": review.body,
        "comments": [
            [c.path, c.line, c.side, c.start_line, c.start_side, c.body] for c in review.comments
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def posting_marker(posting_id: int) -> str:
    """The invisible tag ending every body Jesinia posts: how a review is recognised as
    that attempt's (and never a review the author posted by hand)."""
    return f"<!-- jesinia-posting:{posting_id} -->"


def marked_body(body: str, posting_id: int) -> str:
    """``body`` as sent: with the attempt's marker at its end."""
    marker = posting_marker(posting_id)
    return f"{body}\n\n{marker}" if body else marker


def needs_body(event: ReviewEvent, review: Review) -> bool:
    """GitHub refuses a comment or request for changes with neither a body nor an inline
    comment; an approval may be bare."""
    return event != "APPROVE" and not review.body and not review.comments


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finding_draft(item: object) -> Draft | None:
    if not isinstance(item, Mapping):
        return None
    group, severity, path = item.get("group"), item.get("severity"), item.get("path")
    start, end = _int(item.get("start_line")), _int(item.get("end_line"))
    explanation = item.get("explanation")
    if group not in _GROUPS or severity not in _SEVERITY_TITLES or not isinstance(path, str):
        return None
    if start is None or end is None or not 1 <= start <= end or not isinstance(explanation, str):
        return None
    rule = item.get("rule")
    rule_id = rule.get("id") if isinstance(rule, Mapping) else None
    cited = f"\n\nRule: {rule_id}" if isinstance(rule_id, str) else ""
    text = f"{explanation.strip()}{cited}"
    # Agent output (PR-influenced) never carries the marker's name into a draft (R48).
    text = WITHHELD_TEXT if carries_marker_text(text) else text
    path = WITHHELD_PATH if carries_marker_text(path) else path
    body = f"**{_SEVERITY_TITLES[str(severity)]}** ({group}): {text}"
    ranged = start < end
    try:
        draft = Draft(
            path,
            body[:BODY_MAX],
            line=end,
            side="RIGHT",
            start_line=start if ranged else None,
            start_side="RIGHT" if ranged else None,
            origin_finding=f"{group}/{severity}",
        )
    except InvalidDraftError:
        return None
    return draft


def seed_drafts(findings: Sequence[object]) -> list[Draft]:
    """A round's first drafts, one per well-formed finding (agent output: anything else is
    dropped), each on its lines. Where it posts (inline, or in the body when GitHub can't
    anchor it) is worked out from GitHub's file list each time, never stored."""
    drafts = [_finding_draft(item) for item in findings]
    return [draft for draft in drafts if draft is not None][:DRAFTS_MAX]


# GitHub's limit on a review body, in characters.
GITHUB_BODY_LIMIT: Final = 65_536
# What ``marked_body`` adds at most: the separator and the marker of the largest posting id
# (SQLite's), so a body checked at preview, before the id exists, still fits once posted.
MARKER_RESERVE: Final = len(marked_body("x", 2**63 - 1)) - 1
