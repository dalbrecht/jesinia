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
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from jesinia.services.github.domain.pr import (
    DraftComment,
    InvalidDraftCommentError,
    PrFile,
    ReviewEvent,
    ReviewSide,
)

PostingState = Literal["started", "posted", "failed"]
PostWarning = Literal["blocking_comment", "newer_head", "own_pr_comment_only"]
POST_WARNINGS: Final[tuple[PostWarning, ...]] = (
    "blocking_comment",
    "newer_head",
    "own_pr_comment_only",
)
REVIEW_EVENTS: Final[tuple[ReviewEvent, ...]] = ("APPROVE", "REQUEST_CHANGES", "COMMENT")
# GitHub's limit for a comment or review body is 65,536 characters.
BODY_MAX: Final = 65_000
PATH_MAX: Final = 1024
DRAFTS_MAX: Final = 500
# GitHub's review state for each event, to recognise a review that was already posted.
EVENT_STATES: Final[Mapping[ReviewEvent, str]] = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}

_BLOCKING = re.compile("blocking", re.IGNORECASE)
_HUNK = re.compile(r"^@@ -(\d{1,10})(?:,(\d{1,10}))? \+(\d{1,10})(?:,(\d{1,10}))? @@", re.M)
_SEVERITY_TITLES: Final = {"critical": "Critical", "important": "Important", "minor": "Minor"}
_GROUPS: Final = frozenset({"correctness", "design", "style"})


class PostingDomainError(Exception):
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


class EmptyReview(PostingDomainError):  # noqa: N818 - the error code clients see
    """GitHub needs a body to request changes or comment."""


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


def compose_review(summary: str, drafts: Sequence[Draft], commentable: CommentableLines) -> Review:
    """The review the drafts make: anchored drafts inline, in order; every other draft (an
    outside-the-diff note, or one GitHub can't anchor) in the body after the summary, under
    its file and lines."""
    inline = [draft.comment() for draft in drafts if commentable.anchors(draft)]
    in_body = [draft for draft in drafts if not commentable.anchors(draft)]
    parts = [summary.strip()] if summary.strip() else []
    if in_body:
        notes = "\n\n".join(f"**{_label(draft)}**\n\n{draft.body.strip()}" for draft in in_body)
        parts.append(f"#### Comments outside the diff\n\n{notes}")
    return Review("\n\n".join(parts), tuple(inline))


def has_blocking(body: str, comments: Sequence[DraftComment]) -> bool:
    """Whether the body or any comment says ``blocking``, in any case."""
    return any(_BLOCKING.search(text) for text in (body, *(c.body for c in comments)))


def review_warnings(
    event: ReviewEvent, review: Review, *, own_pr: bool, newer_head: bool
) -> tuple[PostWarning, ...]:
    """The warnings the confirmation shows, in a fixed order."""
    warnings: list[PostWarning] = []
    if event == "APPROVE" and has_blocking(review.body, review.comments):
        warnings.append("blocking_comment")
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


def same_body(posted: str, ours: str) -> bool:
    """Whether a review GitHub holds has the body Jesinia sent (GitHub may change line
    endings and trailing space)."""

    def norm(text: str) -> str:
        return text.replace("\r\n", "\n").strip()

    return norm(posted) == norm(ours)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finding_draft(item: object, commentable: CommentableLines) -> Draft | None:
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
    body = f"**{_SEVERITY_TITLES[str(severity)]}** ({group}): {explanation.strip()}{cited}"
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
    if commentable.anchors(draft):
        return draft
    return Draft(
        path,
        draft.body,
        line=end,
        start_line=draft.start_line,
        origin_finding=draft.origin_finding,
        outside_diff=True,
    )


def seed_drafts(findings: Sequence[object], commentable: CommentableLines) -> list[Draft]:
    """A round's first drafts, one per well-formed finding (agent output: anything else is
    dropped), anchored where GitHub can take them and outside the diff otherwise."""
    drafts = [_finding_draft(item, commentable) for item in findings]
    return [draft for draft in drafts if draft is not None][:DRAFTS_MAX]
