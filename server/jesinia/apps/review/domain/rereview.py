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
import re
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from jesinia.apps.review.domain.coverage import (
    HunkRef,
    ParsedDiff,
    coverage,
    skips_from_json,
    steps_from_json,
    with_clean_step_titles,
)
from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.followup import LineMaps, reanchor
from jesinia.apps.review.domain.posting import Draft, InvalidDraftError, strip_posting_markers
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
_BLANK_LINES: Final = re.compile(r"\n{3,}")


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
    # Where it sits on the head under review (``place``): the path after any rename, the line
    # its text is on now; ``outdated`` when that text changed, so it is never misplaced.
    now_path: str | None = None
    now_line: int | None = None
    now_start_line: int | None = None
    outdated: bool = False
    # The commit it was written on (its line and side are that commit's), and why it couldn't
    # be placed on the head under review when it couldn't (not "changed": unknown).
    commit_sha: str | None = None
    unplaced: str | None = None

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
            "now_path": self.now_path,
            "now_line": self.now_line,
            "now_start_line": self.now_start_line,
            "outdated": self.outdated,
            "commit_sha": self.commit_sha,
            "unplaced": self.unplaced,
        }


@dataclass(frozen=True)
class PreviousStep:
    """A step of the previous round's walkthrough on the head under review: ``now_hunks`` are
    the hunks it walked that are unchanged (in this head's numbering), ``changed`` how many of
    its hunks changed since (or aren't on this head any more)."""

    title: str
    body: str
    now_hunks: tuple[HunkRef, ...]
    changed: int
    unplaced: str | None = None  # why its hunks couldn't be matched to this head's

    def as_json(self) -> dict[str, object]:
        return {
            "title": self.title,
            "body": self.body,
            "now_hunks": [{"path": h.path, "hunk": h.hunk} for h in self.now_hunks],
            "changed": self.changed,
            "unplaced": self.unplaced,
        }


@dataclass(frozen=True)
class PreviousFinding:
    """A finding of the previous round, written at the previous head (``path``, lines) and
    placed on the head under review (``now_*``), or ``outdated`` when its lines changed."""

    group: str
    severity: str
    path: str
    start_line: int
    end_line: int
    explanation: str
    now_path: str | None = None
    now_start_line: int | None = None
    now_end_line: int | None = None
    outdated: bool = False
    unplaced: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "group": self.group,
            "severity": self.severity,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "explanation": self.explanation,
            "now_path": self.now_path,
            "now_start_line": self.now_start_line,
            "now_end_line": self.now_end_line,
            "outdated": self.outdated,
            "unplaced": self.unplaced,
        }


@dataclass(frozen=True)
class PreviousReview:
    """The previous review a re-review builds on: where its comments came from, the head it
    reviewed, (for a stored round) the round, and that round's walkthrough and findings
    placed on the head under review."""

    source: CommentSource
    head_sha: str
    comments: tuple[PreviousComment, ...]
    round_id: int | None = None
    steps: tuple[PreviousStep, ...] = ()
    findings: tuple[PreviousFinding, ...] = ()
    # Why some of it couldn't be placed on the head under review (the causes, joined).
    placement_error: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "placement_error": self.placement_error,
            "source": self.source,
            "head_sha": self.head_sha,
            "round_id": self.round_id,
            "comments": [comment.as_json() for comment in self.comments],
            "steps": [step.as_json() for step in self.steps],
            "findings": [finding.as_json() for finding in self.findings],
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
    threads: Sequence[ReviewThread],
    viewer: str,
    review_body: str = "",
    review_commit: str | None = None,
) -> list[PreviousComment]:
    """The author's comments on GitHub: their review's body first, then each review thread
    they started (resolved ones flagged), in GitHub's order. Each thread is where it was
    written (N1): its original commit, line, range and side; a thread GitHub gives no original
    commit for is taken at ``review_commit`` (the review's), with its reported line."""
    found: list[PreviousComment] = []
    # A review Jesinia posted (whose round is gone) carries its posting marker: not text.
    body = _BLANK_LINES.sub("\n\n", strip_posting_markers(review_body)).strip()
    if body:
        found.append(PreviousComment(f"c{len(found) + 1}", body, commit_sha=review_commit))
    me = viewer.lower()
    for thread in threads:
        if thread.author.lower() != me or not thread.body.strip() or not thread.path:
            continue
        written = thread.original_commit_sha is not None
        line = _int(thread.original_line if written else thread.line)
        start = _int(thread.original_start_line) if written else None
        found.append(
            PreviousComment(
                f"c{len(found) + 1}",
                thread.body,
                path=thread.path,
                line=line,
                side=(thread.side or "RIGHT") if line is not None else None,
                start_line=start
                if line is not None and start is not None and start < line
                else None,
                resolved=thread.resolved,
                commit_sha=thread.original_commit_sha if written else review_commit,
            )
        )
    return found


def _every_hunk(diff: ParsedDiff) -> frozenset[HunkRef]:
    return frozenset(
        HunkRef(f.path, diff.cited_index(position, h))
        for position, f in enumerate(diff.files)
        for h in range(len(f.hunks))
    )


def match_hunks(
    previous: ParsedDiff, current: ParsedDiff, renames: Mapping[str, str]
) -> dict[HunkRef, HunkRef]:
    """Each hunk of ``current`` whose content is in the previous review's diff, with the
    previous hunk it matches (both cited as agents cite them): compared by digest within the
    same file, where a file is the same across a rename (``renames``: old path to new path
    between the two heads, or the rename ``current`` itself shows). Each previous hunk
    matches at most once, in order."""
    seen: dict[str, dict[str, deque[HunkRef]]] = {}
    for position, f in enumerate(previous.files):
        pools = seen.setdefault(f.path, {})
        for h, hunk in enumerate(f.hunks):
            ref = HunkRef(f.path, previous.cited_index(position, h))
            pools.setdefault(hunk.digest, deque()).append(ref)
    came_from = {new: old for old, new in renames.items()}
    matched: dict[HunkRef, HunkRef] = {}
    for position, f in enumerate(current.files):
        candidates = [f.path, came_from.get(f.path), f.old_path]
        found = [seen[path] for path in dict.fromkeys(candidates) if path and path in seen]
        for h, hunk in enumerate(f.hunks):
            pool = next((p[hunk.digest] for p in found if p.get(hunk.digest)), None)
            if pool is not None:
                matched[HunkRef(f.path, current.cited_index(position, h))] = pool.popleft()
    return matched


def changed_hunks(
    previous: ParsedDiff | None, current: ParsedDiff, renames: Mapping[str, str]
) -> frozenset[HunkRef]:
    """The hunks of ``current`` whose content isn't in the previous review's diff
    (``match_hunks``). With no previous diff (it couldn't be rebuilt), every hunk is new."""
    every = _every_hunk(current)
    if previous is None:
        return every
    return every - match_hunks(previous, current, renames).keys()


def _moved(
    path: str,
    line: int,
    side: ReviewSide,
    start_line: int | None,
    maps: LineMaps,
    renamed: Mapping[tuple[str, ReviewSide], str],
) -> Draft | None:
    """The position on the head under review by ``reanchor``'s rules (Task 11), or ``None``
    when its text changed, or it was never a position GitHub takes (a malformed range)."""
    try:
        draft = Draft(
            path,
            "placed",
            line=line,
            side=side,
            start_line=start_line,
            start_side=side if start_line is not None else None,
            outdated=True,
        )
    except InvalidDraftError:
        return None
    placed = reanchor(draft, maps, renamed)
    return None if placed.outdated else placed


def place(
    comment: PreviousComment,
    maps: LineMaps,
    renamed: Mapping[tuple[str, ReviewSide], str],
) -> PreviousComment:
    """``comment`` on the head under review, by the same rules as carried drafts: moved where
    every line of it has unchanged text, after any rename; otherwise outdated, left where it
    was written. A file-level comment follows the rename only."""
    if comment.path is None:
        return comment
    side: ReviewSide = comment.side or "RIGHT"
    now_path = renamed.get((comment.path, side), comment.path)
    if comment.line is None:
        return replace(comment, now_path=now_path)
    moved = _moved(comment.path, comment.line, side, comment.start_line, maps, renamed)
    if moved is None:
        return replace(comment, now_path=now_path, now_line=None, outdated=True)
    return replace(
        comment, now_path=moved.path, now_line=moved.line, now_start_line=moved.start_line
    )


def previous_findings(
    artifacts: Sequence[object],
    maps: LineMaps,
    renamed: Mapping[tuple[str, ReviewSide], str],
    *,
    unplaced: str | None = None,
) -> tuple[PreviousFinding, ...]:
    """The previous round's findings (its ``correctness`` and ``findings`` artifacts, each
    finding once), placed on the head under review by their new-file lines; outdated where
    those lines changed; ``unplaced`` (the cause) when nothing could be mapped. Malformed
    entries are skipped."""
    found: list[PreviousFinding] = []
    seen: set[tuple[object, ...]] = set()
    for artifact in artifacts:
        items = artifact.get("findings") if isinstance(artifact, Mapping) else None
        for item in items if isinstance(items, list) else []:
            entry: Mapping[str, object] = item if isinstance(item, Mapping) else {}
            path, explanation = entry.get("path"), entry.get("explanation")
            start, end = _int(entry.get("start_line")), _int(entry.get("end_line"))
            group, severity = entry.get("group"), entry.get("severity")
            key = (group, severity, path, start, end, explanation)
            valid = isinstance(path, str) and path and isinstance(explanation, str)
            if not valid or start is None or end is None or end < start or key in seen:
                continue
            seen.add(key)
            path, explanation = str(path), str(explanation)
            if unplaced is not None:
                found.append(
                    PreviousFinding(
                        str(group), str(severity), path, start, end, explanation,
                        now_path=path, unplaced=unplaced,
                    )
                )  # fmt: skip
                continue
            moved = _moved(path, end, "RIGHT", start if start < end else None, maps, renamed)
            found.append(
                PreviousFinding(
                    str(group),
                    str(severity),
                    path,
                    start,
                    end,
                    explanation,
                    now_path=renamed.get((path, "RIGHT"), path),
                    now_start_line=None if moved is None else (moved.start_line or moved.line),
                    now_end_line=None if moved is None else moved.line,
                    outdated=moved is None,
                )
            )
    return tuple(found)


def previous_steps(
    walkthrough: object, matches: Mapping[HunkRef, HunkRef], *, unplaced: str | None = None
) -> tuple[PreviousStep, ...]:
    """The previous round's walkthrough steps on the head under review: each hunk they cite
    (previous numbering) as the unchanged hunk it is now (``matches``: previous to current);
    the rest are counted as changed."""
    raw = walkthrough.get("steps") if isinstance(walkthrough, Mapping) else None
    items = [item for item in (raw if isinstance(raw, list) else []) if isinstance(item, Mapping)]
    steps: list[PreviousStep] = []
    for item, step in zip(items, steps_from_json(walkthrough), strict=True):
        body = item.get("body")
        now = tuple(matches[ref] for ref in step.hunks if ref in matches)
        changed = 0 if unplaced is not None else len(step.hunks) - len(now)
        text = body if isinstance(body, str) else ""
        steps.append(PreviousStep(step.title, text, now, changed, unplaced))
    return tuple(steps)


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
    new = set(new_hunks)
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
                # Only new hunks can be what addressed a comment (spec §4.2, M6).
                "hunks": [_ref_json(h) for h in _cited(diff, entry.get("hunks")) if h in new],
            }
        )
    walk = output.get("new_changes_walkthrough")
    # Titles are cleaned by the agent's own step positions, before dropping renumbers them.
    walk = with_clean_step_titles(walk) if isinstance(walk, Mapping) else {}
    raw_steps = walk.get("steps")
    kept = [
        item
        for item, step in zip(
            raw_steps if isinstance(raw_steps, list) else [], steps_from_json(walk), strict=False
        )
        if step.hunks and all(ref in new for ref in step.hunks)
    ]
    # The agent's skips are kept only where they name a new hunk, or a file with one (M5).
    new_paths = {ref.path for ref in new}
    skips = [
        skip
        for skip in skips_from_json(walk)
        if (skip.path in new_paths if skip.hunk is None else HunkRef(skip.path, skip.hunk) in new)
    ]
    skipped = [{"path": k.path, "hunk": k.hunk, "reason": k.reason} for k in skips]
    walked = coverage(only_hunks(diff, new), steps_from_json({"steps": kept}), skips)
    return {
        "requests": requests,
        "unanswered": [c.ref for c in previous.comments if c.ref not in answered],
        "dropped_requests": dropped,
        "previous_round_id": previous.round_id,
        "new_hunks": [_ref_json(ref) for ref in _sorted(new)],
        "new_changes_walkthrough": {"steps": kept, "skipped": skipped},
        "coverage": walked.as_json(),
    }
