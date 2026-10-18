# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental re-reviews (spec §4.2). Pure: no I/O.

A re-review builds on the author's previous review: their comments (as posted from a stored
round, or their review threads on GitHub) and only the hunks that changed since. Hunks are
compared by content (``Hunk.digest``: lines and context, whitespace verbatim, no line
numbers), following renames between the two heads; anything that can't be compared counts
as changed, so nothing new is ever hidden. Unchanged hunks keep R42's numbering and are
marked, never dropped (R69).

The agent's ``rereview`` answer is advice: ``rereview_artifact`` keeps only requests about
comments that exist (each once), only hunks the diff has, and only walkthrough steps on new
hunks; a comment it gave no status is listed as unanswered, never guessed.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from jesinia.apps.review.domain.coverage import (
    HunkRef,
    ParsedDiff,
    coverage,
    skips_from_json,
    steps_from_json,
)
from jesinia.apps.review.domain.errors import ReviewError
from jesinia.services.github.domain.pr import ReviewSide, ReviewThread

RequestStatus = Literal["addressed", "partly", "not_addressed"]
REQUEST_STATUSES: Final[tuple[RequestStatus, ...]] = ("addressed", "partly", "not_addressed")
CommentSource = Literal["round", "github"]
COMMENT_SOURCES: Final[tuple[CommentSource, ...]] = ("round", "github")
# Follow-up kinds that get a re-review (spec §4.1 changed rebases, §4.2 re-reviews).
REREVIEW_KINDS: Final = frozenset({"rereview", "changed_hunks_only"})
# Characters of the new changes and of the previous comments one re-review job is given;
# what doesn't fit is left out whole (a hunk, a comment) and counted in Jesinia's notes.
NEW_CHANGES_CHARS: Final = 120_000
COMMENTS_CHARS: Final = 60_000
_SIDES: Final = ("LEFT", "RIGHT")


class RereviewError(ReviewError):
    """Base for a re-review Jesinia can't build."""


class InvalidPreviousCommentError(RereviewError):
    """A previous comment with impossible values."""


class RereviewInputError(RereviewError):
    """The previous review or the new changes couldn't be read (the cause is chained)."""


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise InvalidPreviousCommentError(message)


@dataclass(frozen=True)
class PreviousComment:
    """One of the author's comments in their previous review: ``ref`` is how the agent cites
    it (``c1``…, Jesinia's numbering); ``path`` and the lines are where it was written (at the
    previous head), or ``None`` for the review's general comment.

    Raises:
        InvalidPreviousCommentError: an empty ref or body, a line below 1, an unknown side.
    """

    ref: str
    body: str
    path: str | None = None
    line: int | None = None
    side: ReviewSide | None = None
    start_line: int | None = None
    resolved: bool = False

    def __post_init__(self) -> None:
        _check(bool(self.ref), "a previous comment needs a ref")
        _check(bool(self.body.strip()), "a previous comment needs a body")
        _check(self.line is None or self.line >= 1, "a line is 1 or more")
        _check(self.start_line is None or self.start_line >= 1, "a start line is 1 or more")
        _check(self.side is None or self.side in _SIDES, f"not a side: {self.side!r}")

    def label(self) -> str:
        """Where the comment was written, in Jesinia's words (``a.py:3``, ``a.py:1–3 (old)``)."""
        if self.path is None:
            return "the review"
        if self.line is None:
            return self.path
        lines = f"{self.start_line}–{self.line}" if self.start_line else f"{self.line}"
        return f"{self.path}:{lines}{' (old)' if self.side == 'LEFT' else ''}"

    def as_json(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "body": self.body,
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "start_line": self.start_line,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class PreviousReview:
    """The previous review a re-review builds on: where its comments came from, the head it
    reviewed, and (for a stored round) the round."""

    source: CommentSource
    head_sha: str
    comments: tuple[PreviousComment, ...]
    round_id: int | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "head_sha": self.head_sha,
            "round_id": self.round_id,
            "comments": [comment.as_json() for comment in self.comments],
        }


@dataclass(frozen=True)
class RereviewInput:
    """What the ``rereview`` job is given: the previous review, the hunks new since (in the
    round's numbering), their text (capped), and Jesinia's notes on what was cut."""

    previous: PreviousReview
    new_hunks: tuple[HunkRef, ...]
    new_changes: str
    notes: tuple[str, ...] = ()


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _side(value: object) -> ReviewSide | None:
    return "LEFT" if value == "LEFT" else "RIGHT" if value == "RIGHT" else None


def comments_from_posting(body: str, comments: Sequence[object]) -> list[PreviousComment]:
    """A posted review's comments as posted: its body (the general comment) first, then each
    inline comment (``DraftComment`` as stored with the posting). A malformed position is
    dropped, the text kept; an entry without a path or body is skipped."""
    found: list[PreviousComment] = []
    if body.strip():
        found.append(PreviousComment(f"c{len(found) + 1}", body.strip()))
    for item in comments:
        if not isinstance(item, Mapping):
            continue
        path, text = item.get("path"), item.get("body")
        if not isinstance(path, str) or not path or not isinstance(text, str) or not text.strip():
            continue
        line, side = _int(item.get("line")), _side(item.get("side"))
        placed = line is not None and side is not None
        found.append(
            PreviousComment(
                f"c{len(found) + 1}",
                text,
                path=path,
                line=line if placed else None,
                side=side if placed else None,
                start_line=_int(item.get("start_line")) if placed else None,
            )
        )
    return found


def comments_from_threads(
    threads: Sequence[ReviewThread], viewer: str, review_body: str = ""
) -> list[PreviousComment]:
    """The author's comments on GitHub: their review's body first, then each review thread
    they started (resolved ones flagged), in GitHub's order."""
    found: list[PreviousComment] = []
    if review_body.strip():
        found.append(PreviousComment(f"c{len(found) + 1}", review_body.strip()))
    me = viewer.lower()
    for thread in threads:
        if thread.author.lower() != me or not thread.body.strip() or not thread.path:
            continue
        found.append(
            PreviousComment(
                f"c{len(found) + 1}",
                thread.body,
                path=thread.path,
                line=_int(thread.line),
                side="RIGHT" if _int(thread.line) is not None else None,
                resolved=thread.resolved,
            )
        )
    return found


def _every_hunk(diff: ParsedDiff) -> frozenset[HunkRef]:
    return frozenset(
        HunkRef(f.path, diff.cited_index(position, h))
        for position, f in enumerate(diff.files)
        for h in range(len(f.hunks))
    )


def changed_hunks(
    previous: ParsedDiff | None, current: ParsedDiff, renames: Mapping[str, str]
) -> frozenset[HunkRef]:
    """The hunks of ``current`` (cited as agents cite them) whose content isn't in the
    previous review's diff: compared by digest within the same file, where a file is the same
    across a rename (``renames``: old path to new path between the two heads, or the rename
    ``current`` itself shows). Each previous hunk matches at most once. With no previous diff
    (it couldn't be rebuilt), every hunk is new."""
    if previous is None:
        return _every_hunk(current)
    seen: dict[str, Counter[str]] = {}
    for f in previous.files:
        seen.setdefault(f.path, Counter()).update(h.digest for h in f.hunks)
    came_from = {new: old for old, new in renames.items()}
    changed: set[HunkRef] = set()
    for position, f in enumerate(current.files):
        candidates = [f.path, came_from.get(f.path), f.old_path]
        pools = [seen[path] for path in dict.fromkeys(candidates) if path and path in seen]
        for h, hunk in enumerate(f.hunks):
            pool = next((p for p in pools if p[hunk.digest] > 0), None)
            if pool is None:
                changed.add(HunkRef(f.path, current.cited_index(position, h)))
            else:
                pool[hunk.digest] -= 1
    return frozenset(changed)


def only_hunks(diff: ParsedDiff, refs: Collection[HunkRef]) -> ParsedDiff:
    """``diff`` with every hunk not in ``refs`` marked unchanged (numbers kept). An
    incomplete diff is returned as it is: what it lacks can't be told apart."""
    if not diff.complete:
        return diff
    wanted = {diff.locate(ref.path, ref.hunk) for ref in refs}
    unchanged = frozenset(
        (position, h)
        for position, f in enumerate(diff.files)
        for h in range(len(f.hunks))
        if (position, h) not in wanted
    )
    return replace(diff, unchanged=unchanged)


def _sorted(refs: Collection[HunkRef]) -> list[HunkRef]:
    return sorted(refs, key=lambda ref: (ref.path, ref.hunk))


def new_changes_text(
    diff: ParsedDiff, refs: Collection[HunkRef], limit: int = NEW_CHANGES_CHARS
) -> tuple[str, int]:
    """The text of the hunks in ``refs`` (from a diff parsed with ``keep_text``), each under
    its path (JSON-quoted: PR content) and number, within ``limit`` characters; returns it
    and how many hunks were left out for the limit."""
    blocks: list[str] = []
    used = cut = 0
    for ref in _sorted(refs):
        slot = diff.locate(ref.path, ref.hunk)
        if slot is None:
            continue
        hunk = diff.files[slot[0]].hunks[slot[1]]
        block = f"{json.dumps(ref.path, ensure_ascii=False)} hunk {ref.hunk}\n{hunk.text or ''}"
        if used + len(block) > limit:
            cut += 1
            continue
        blocks.append(block)
        used += len(block) + 2
    if not blocks and not cut:
        return "No hunk changed since your last review.", 0
    return "\n\n".join(blocks), cut


def comments_listing(
    comments: Sequence[PreviousComment], limit: int = COMMENTS_CHARS
) -> tuple[str, int]:
    """The previous comments as the agent reads them (``[c1] a.py:3`` then the text), within
    ``limit`` characters; returns it and how many were left out for the limit."""
    blocks: list[str] = []
    used = cut = 0
    for comment in comments:
        resolved = " (resolved on GitHub)" if comment.resolved else ""
        block = f"[{comment.ref}] {comment.label()}{resolved}\n{comment.body}"
        if used + len(block) > limit:
            cut += 1
            continue
        blocks.append(block)
        used += len(block) + 2
    if not blocks and not cut:
        return "Your previous review left no comments.", 0
    return "\n\n".join(blocks), cut


def _cited(diff: ParsedDiff, hunks: object) -> list[HunkRef]:
    """The well-formed hunks in ``hunks`` that ``diff`` has (Jesinia's numbering)."""
    refs: list[HunkRef] = []
    for item in hunks if isinstance(hunks, list) else []:
        path = item.get("path") if isinstance(item, Mapping) else None
        index = item.get("hunk") if isinstance(item, Mapping) else None
        if not isinstance(path, str) or not isinstance(index, int) or isinstance(index, bool):
            continue
        if diff.locate(path, index) is not None:
            refs.append(HunkRef(path, index))
    return refs


def _ref_json(ref: HunkRef) -> dict[str, object]:
    return {"path": ref.path, "hunk": ref.hunk}


def rereview_artifact(
    output: Mapping[str, object],
    previous: PreviousReview,
    diff: ParsedDiff,
    new_hunks: Collection[HunkRef],
) -> dict[str, object]:
    """The ``rereview`` artifact from the agent's schema-valid ``output``: each request about
    a known comment (the first one per comment), its hunks only where ``diff`` has them; the
    comments it gave no status (``unanswered``); the walkthrough of the new changes with only
    steps whose every hunk is new; and Jesinia's coverage of the new changes."""
    known = {comment.ref for comment in previous.comments}
    requests: list[dict[str, object]] = []
    answered: set[str] = set()
    dropped = 0
    raw = output.get("requests")
    for item in raw if isinstance(raw, list) else []:
        entry: Mapping[str, object] = item if isinstance(item, Mapping) else {}
        ref, status = entry.get("comment_ref"), entry.get("status")
        valid = isinstance(ref, str) and ref in known and status in REQUEST_STATUSES
        if not valid or ref in answered:
            dropped += 1
            continue
        comment_ref = str(ref)
        answered.add(comment_ref)
        explanation = entry.get("explanation")
        requests.append(
            {
                "comment_ref": comment_ref,
                "status": status,
                "explanation": explanation if isinstance(explanation, str) else "",
                "hunks": [_ref_json(h) for h in _cited(diff, entry.get("hunks"))],
            }
        )
    walk = output.get("new_changes_walkthrough")
    walk = walk if isinstance(walk, Mapping) else {}
    new = set(new_hunks)
    raw_steps = walk.get("steps")
    kept = [
        item
        for item, step in zip(
            raw_steps if isinstance(raw_steps, list) else [], steps_from_json(walk), strict=False
        )
        if step.hunks and all(ref in new for ref in step.hunks)
    ]
    walked = coverage(
        only_hunks(diff, new), steps_from_json({"steps": kept}), skips_from_json(walk)
    )
    return {
        "requests": requests,
        "unanswered": [c.ref for c in previous.comments if c.ref not in answered],
        "dropped_requests": dropped,
        "previous_round_id": previous.round_id,
        "new_hunks": [_ref_json(ref) for ref in _sorted(new)],
        "new_changes_walkthrough": {"steps": kept, "skipped": walk.get("skipped", [])},
        "coverage": walked.as_json(),
    }
