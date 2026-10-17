# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow-ups on a PR's new head (spec §4.1 and §7 "When the head changes").

Pure: no I/O. ``classify`` decides what a new head of a PR the author reviewed before needs:
nothing (a rebase of an approval with the same per-file patch IDs and CI green or running:
**still approved**), a re-review of only the files whose patch IDs differ, or a re-review.
``reanchor`` moves a draft carried over from an earlier head to the line its text is on now,
or flags it ``outdated`` when that line's text changed: it is never guessed.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Literal, Protocol

from jesinia.apps.review.domain.coverage import ParsedDiff
from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.gates import CiState
from jesinia.apps.review.domain.posting import Draft
from jesinia.apps.review.domain.workspace import InvalidShaError, check_sha
from jesinia.services.github.domain.pr import ReviewEvent, ReviewSide

if TYPE_CHECKING:  # the queue's row imports this module for its kind
    from jesinia.apps.review.domain.queue import PrKind

FollowUpKind = Literal["new", "still_approved", "rereview", "changed_hunks_only"]
FOLLOW_UP_KINDS: Final[tuple[FollowUpKind, ...]] = (
    "new",
    "still_approved",
    "rereview",
    "changed_hunks_only",
)
PrevVerdict = Literal["approved", "changes_requested", "commented"]
PREV_VERDICTS: Final[tuple[PrevVerdict, ...]] = ("approved", "changes_requested", "commented")
PrevSource = Literal["round", "github"]
PREV_SOURCES: Final[tuple[PrevSource, ...]] = ("round", "github")
# Spec §4.1: "CI on the new head is green, or still running". A head with no CI at all
# doesn't block either (ruling R28): the approval was given without CI too.
_CI_KEEPS_APPROVAL: Final[frozenset[str]] = frozenset({"passing", "pending", "none"})
_STATE_VERDICTS: Final[Mapping[str, PrevVerdict]] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
}
_EVENT_VERDICTS: Final[Mapping[ReviewEvent, PrevVerdict]] = {
    "APPROVE": "approved",
    "REQUEST_CHANGES": "changes_requested",
    "COMMENT": "commented",
}
_PR_KINDS: Final[Mapping[FollowUpKind, PrKind]] = {
    "new": "new",
    "still_approved": "rebase",
    "rereview": "rereview",
    "changed_hunks_only": "rereview",
}
# Per (path, side): which old line number holds the same text at which new line number;
# ``None`` when the file couldn't be read at one of the two commits.
LineMaps = Mapping[tuple[str, ReviewSide], Mapping[int, int] | None]


class FollowUpError(ReviewError):
    """Base for follow-up values Jesinia refuses."""


class InvalidPrevReviewError(FollowUpError):
    """A previous review or a follow-up with impossible values."""


def _sha(value: str, what: str) -> None:
    try:
        check_sha(value)
    except InvalidShaError as exc:
        raise InvalidPrevReviewError(f"{what} must be a full commit SHA: {exc}") from exc


@dataclass(frozen=True)
class PrevReview:
    """The author's latest review of the PR, at an earlier head: from a stored round
    (``round_id``) or posted on GitHub. ``merge_base`` is computed (the reviewed head's
    merge base with the base it was reviewed on); ``None`` when that commit can't be read
    any more, and then nothing is assumed unchanged.

    Raises:
        InvalidPrevReviewError: a SHA that isn't one, an unknown verdict or source.
    """

    head_sha: str
    merge_base: str | None
    verdict: PrevVerdict
    source: PrevSource
    round_id: int | None = None

    def __post_init__(self) -> None:
        _sha(self.head_sha, "the reviewed head")
        if self.merge_base is not None:
            _sha(self.merge_base, "the merge base")
        if self.verdict not in PREV_VERDICTS:
            raise InvalidPrevReviewError(f"not a review verdict: {self.verdict!r}")
        if self.source not in PREV_SOURCES:
            raise InvalidPrevReviewError(f"not a review source: {self.source!r}")


@dataclass(frozen=True)
class FollowUp:
    """What a new head needs. ``differing_paths``: the files whose patch IDs differ from the
    previous review's (the only ones a ``changed_hunks_only`` round reviews); ``note``: one
    line for the queue row, in Jesinia's words (it names no path: paths are PR text).
    ``error``: the head couldn't be checked (GitHub or git failed); it is checked again on
    the next sync, and meanwhile counts as an ordinary new head (``pr_kind`` "new")."""

    kind: FollowUpKind
    differing_paths: list[str] = field(default_factory=list)
    note: str = ""
    prev_head: str | None = None
    prev_source: PrevSource | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in FOLLOW_UP_KINDS:
            raise InvalidPrevReviewError(f"not a follow-up kind: {self.kind!r}")


class HasHead(Protocol):
    """The PR as ``classify`` needs it."""

    @property
    def head_sha(self) -> str: ...


def verdict_of_review_state(state: str) -> PrevVerdict | None:
    """A GitHub review state as a verdict; ``None`` for one that isn't the author's verdict
    (dismissed, or still pending)."""
    return _STATE_VERDICTS.get(state)


def verdict_of_event(event: ReviewEvent) -> PrevVerdict:
    """The verdict a review Jesinia posted with ``event`` gave."""
    return _EVENT_VERDICTS[event]


def pr_kind(followup: FollowUp | None) -> PrKind:
    """The queue row's kind (spec §2): a still-approved rebase is ``rebase``, any re-review
    ``rereview``."""
    if followup is None or followup.error is not None:
        return "new"
    return _PR_KINDS[followup.kind]


def differing_paths(old: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    """The paths whose patch IDs differ, including files only one side changes."""
    return sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))


def _files(count: int) -> str:
    return f"{count} file{'s' if count != 1 else ''}"


def classify(
    prev: PrevReview | None,
    pr: HasHead,
    patch_ids_old: Mapping[str, str] | None,
    patch_ids_new: Mapping[str, str] | None,
    ci: CiState,
) -> FollowUp:
    """What ``pr``'s head needs after ``prev`` (spec §4.1): ``compare`` with the CI rule
    applied (``effective_followup``).

    ``patch_ids_old``: per-file patch IDs of the reviewed diff (merge base to reviewed head);
    ``patch_ids_new``: the same for the head now. Either is ``None`` when it couldn't be
    computed: nothing is then assumed unchanged (Review Focus 1).
    """
    return effective_followup(compare(prev, pr, patch_ids_old, patch_ids_new), ci)


def effective_followup(followup: FollowUp, ci: CiState) -> FollowUp:
    """``followup`` under the head's CI now (C2): same content as the approval stays still
    approved only while CI is green, running or absent; a failing CI makes it a re-review.
    Applied each time a follow-up is read, so CI that fails later is never missed."""
    if followup.kind != "still_approved" or followup.error is not None:
        return followup
    if ci in _CI_KEEPS_APPROVAL:
        return followup
    note = "Same changes as you approved, but CI is failing"
    return FollowUp("rereview", [], note, followup.prev_head, followup.prev_source)


def compare(
    prev: PrevReview | None,
    pr: HasHead,
    patch_ids_old: Mapping[str, str] | None,
    patch_ids_new: Mapping[str, str] | None,
) -> FollowUp:
    """What ``pr``'s head needs by content alone: the result stored per head. CI is not
    part of it (``effective_followup`` applies it when read)."""
    if prev is None or prev.head_sha == pr.head_sha:
        return FollowUp("new")
    if patch_ids_old is None or patch_ids_new is None:
        note = "New commits since your review; the reviewed diff couldn't be rebuilt to compare"
        return _after(prev, "rereview", [], note)
    differing = differing_paths(patch_ids_old, patch_ids_new)
    if prev.verdict != "approved":
        note = f"New commits since your review: {_files(len(differing))} changed"
        return _after(prev, "rereview", differing, note)
    if differing:
        note = f"Rebased with changes since your approval: {_files(len(differing))} to review"
        return _after(prev, "changed_hunks_only", differing, note)
    note = f"Still approved: a rebase with the same changes as {prev.head_sha[:12]}"
    return _after(prev, "still_approved", [], note)


def _after(prev: PrevReview, kind: FollowUpKind, differing: list[str], note: str) -> FollowUp:
    return FollowUp(kind, differing, note, prev.head_sha, prev.source)


def restrict_diff(diff: ParsedDiff, paths: Collection[str]) -> ParsedDiff:
    """``diff`` with only the files in ``paths`` (a re-review of what changed since the
    approval). An incomplete diff is left whole: what it lacks can't be told apart."""
    if not diff.complete:
        return diff
    wanted = set(paths)
    return replace(diff, files=tuple(f for f in diff.files if f.path in wanted))


def line_map(old_text: str, new_text: str) -> dict[int, int]:
    """For each line of ``old_text`` whose text is unchanged in ``new_text`` (as a diff of
    the two matches it), its new line number; both 1-based."""
    old, new = old_text.splitlines(), new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    mapped: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapped[block.a + offset + 1] = block.b + offset + 1
    return mapped


def _neighbour_moved(mapped: Mapping[int, int], line: int) -> bool:
    """Whether the line before or after ``line`` maps right next to where ``line`` maps."""
    return mapped.get(line - 1) == mapped[line] - 1 or mapped.get(line + 1) == mapped[line] + 1


def reanchor(draft: Draft, maps: LineMaps) -> Draft:
    """``draft`` on the new head: moved to where its lines' text is now, when every line of
    it (its whole range) is unchanged and still consecutive; otherwise kept where it was and
    flagged ``outdated``. A single line moves only with a neighbour that moved with it, so
    a common line (a blank, a brace) matched by coincidence in rewritten code isn't trusted.
    A draft without a line (a general note) is unchanged. An
    outside-the-diff draft's line only labels it: it is mapped by its right side."""
    if draft.line is None:
        return draft
    side: ReviewSide = draft.side or "RIGHT"
    start = draft.start_line or draft.line
    mapped = maps.get((draft.path, side))
    same_side = draft.start_side in (None, side)
    lines = range(start, draft.line + 1)
    if mapped is None or not same_side or any(line not in mapped for line in lines):
        return replace(draft, outdated=True)
    if mapped[draft.line] - mapped[start] != draft.line - start:
        return replace(draft, outdated=True)  # lines were inserted inside the range
    if start == draft.line and not _neighbour_moved(mapped, draft.line):
        return replace(draft, outdated=True)
    return replace(
        draft,
        line=mapped[draft.line],
        start_line=None if draft.start_line is None else mapped[draft.start_line],
        outdated=False,
    )
