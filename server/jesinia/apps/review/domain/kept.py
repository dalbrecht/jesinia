# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kept review pages and the repo's "How it works" page (spec §6.2, §8; plan Task 16).

A deep review the author finishes is kept as ``<reviews folder>/<owner>-<repo>/#<n> <title>.md``
(``folders.review`` in ``config.toml``, ``Reviews`` by default):
one page per PR, with one section per kept round between ``jesinia:review-round`` markers,
so keeping a round again replaces its own section and leaves the author's edits elsewhere
alone. Everything Jesinia owns in the front matter sits under ``jesinia:`` (foundation
§5.2). Agent text is wrapped in provenance, and every piece of text that came from the PR,
the agent or the author is made inert, so none of it can open or close a marker.

``How it works.md`` beside it describes the system. It is the author's page: Jesinia only
drafts a seed or an update, and writes it when the author accepts (spec §6.2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final, Literal, cast

from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.workspace import parse_repo
from jesinia.services.kb.domain.provenance import inert, provenance_label, wrap_ai
from jesinia.services.kb.domain.tags import render_tag_line

REVIEWS_FOLDER: Final = "Reviews"  # the default; ``folders.review`` overrides it
REVIEW_FOLDER_KIND: Final = "review"  # the ``folders.review`` config key
SYSTEM_PAGE_TITLE: Final = "How it works"
REVIEW_KIND: Final = "review/pr"
SYSTEM_KIND: Final = "review/system"
# The round's artifacts these records are stored as (the PWA reads them with the round).
KEPT: Final = "kept"
SYSTEM_DRAFT: Final = "system_draft"
REVIEW_TAG: Final = "code-review"
# Bounds on what a page lists: a huge PR still keeps a readable page.
CODE_MAX: Final = 200
FINDINGS_MAX: Final = 30
STEPS_MAX: Final = 60
COMMENTS_MAX: Final = 60
TITLE_MAX: Final = 200
# Rounds a follow-up classified as a rebase aren't kept (spec §8): nothing new was reviewed.
REBASE_KINDS: Final = frozenset({"still_approved", "changed_hunks_only"})

_SEVERITY_RANK: Final = {"critical": 0, "important": 1, "minor": 2}
_VERDICT_WORDS: Final = {
    "approve": "approve",
    "request_changes": "request changes",
    "needs_closer_look": "needs a closer look",
}
_OUTCOME_WORDS: Final = {
    "approve": "Approved on GitHub",
    "request_changes": "Changes requested on GitHub",
    "comment": "Commented on GitHub",
    "done": "Done without posting",
}
_GITHUB_URL = re.compile(r"https://github\.com/[^\s<>()\[\]`]+")

KeptOutcome = Literal["kept", "failed"]
DraftKind = Literal["seed", "update"]
# ``withdrawn``: nothing to offer any more (a seed once the page exists, or no update).
DraftState = Literal["proposed", "accepted", "failed", "withdrawn"]


class KeptPageError(ReviewError):
    """Base for the errors of kept review pages and the "How it works" page."""


class NotKeepable(KeptPageError):  # noqa: N818 - the error code clients see
    """The round isn't one Jesinia keeps: quick, a rebase, or not finished (posted or Done)."""


class NoSystemDraft(KeptPageError):  # noqa: N818 - the error code clients see
    """The round has no drafted "How it works" seed or update to accept."""


class SystemPageChanged(KeptPageError):  # noqa: N818 - the error code clients see
    """The "How it works" page changed since the draft was made (or since the author was
    shown it): nothing was written, so no one's edit is lost."""


@dataclass(frozen=True)
class Provenance:
    """Who wrote the agent text of a round: the job runner, its model, the session."""

    agent: str
    model: str
    session: str
    at: datetime

    def wrap(self, text: str) -> str:
        """``text`` (made inert) inside a provenance block."""
        return wrap_ai(
            inert(text.strip()),
            agent=provenance_label(self.agent),
            model=provenance_label(self.model),
            at=self.at,
            session=provenance_label(self.session),
        )


@dataclass(frozen=True)
class Authors:
    """Who wrote each agent-written part of a round's section: the jobs that produced it."""

    overview: Provenance
    walkthrough: Provenance
    findings: Provenance


@dataclass(frozen=True)
class KeptComment:
    """One of the author's comments: where it is anchored and what it says."""

    path: str
    line: int | None
    body: str


@dataclass(frozen=True)
class KeptRound:
    """What a kept round says, read from the round and its artifacts.

    ``outcome`` is the posted event in lowercase (``approve``, ``request_changes``,
    ``comment``) or ``done``; ``overview`` the agent's purpose; ``steps`` the walkthrough's
    step titles; ``summary`` and ``comments`` the author's own words.
    """

    round_id: int
    repo: str
    number: int
    title: str
    url: str
    head_sha: str
    verdict: str | None
    reviewed: datetime
    outcome: str
    review_url: str | None = None
    overview: str = ""
    steps: tuple[str, ...] = ()
    findings: tuple[Mapping[str, object], ...] = ()
    summary: str = ""
    comments: tuple[KeptComment, ...] = ()
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeptState:
    """Where a round's page went (``kept``), or why it couldn't be kept (``failed``)."""

    state: KeptOutcome
    page_id: str | None
    path: str | None
    error: str | None

    def as_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "page_id": self.page_id,
            "path": self.path,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: object) -> KeptState | None:
        """The stored record, or ``None`` when there is none (or it isn't one)."""
        if not isinstance(data, Mapping) or data.get("state") not in ("kept", "failed"):
            return None
        return cls(
            cast(KeptOutcome, data["state"]),
            _text_or_none(data.get("page_id")),
            _text_or_none(data.get("path")),
            _text_or_none(data.get("error")),
        )


@dataclass(frozen=True)
class SystemDraft:
    """A drafted "How it works" seed (no page yet) or update, and the page revision it was
    made against (``None`` for a seed): accepting it is refused once the page has moved on."""

    kind: DraftKind
    text: str
    base_rev: str | None
    page_id: str | None
    state: DraftState
    error: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "text": self.text,
            "base_rev": self.base_rev,
            "page_id": self.page_id,
            "state": self.state,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: object) -> SystemDraft | None:
        """The stored draft, or ``None`` when there is none (or it isn't one)."""
        if not isinstance(data, Mapping):
            return None
        kind, state = data.get("kind"), data.get("state")
        if kind not in ("seed", "update") or state not in (
            "proposed",
            "accepted",
            "failed",
            "withdrawn",
        ):
            return None
        text = data.get("text")
        return cls(
            cast(DraftKind, kind),
            text if isinstance(text, str) else "",
            _text_or_none(data.get("base_rev")),
            _text_or_none(data.get("page_id")),
            cast(DraftState, state),
            _text_or_none(data.get("error")),
        )


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


# --- where pages go -----------------------------------------------------------------------------


def repo_folder(repo: str, base: str = REVIEWS_FOLDER) -> PurePosixPath:
    """``<base>/<owner>-<repo>``, in the repo's canonical (lowercase) spelling. The per-repo
    folder keeps each repo's "How it works" page apart from the others'.

    Raises:
        InvalidRepoError: ``repo`` isn't ``owner/name``.
    """
    owner, name = parse_repo(repo)
    return PurePosixPath(base, f"{owner}-{name}")


def system_page_path(repo: str, base: str = REVIEWS_FOLDER) -> PurePosixPath:
    """The repo's ``How it works.md``."""
    return repo_folder(repo, base) / f"{SYSTEM_PAGE_TITLE}.md"


def system_source(repo: str) -> str:
    """The ``jesinia.source`` a How it works page carries: it finds the repo's page again
    wherever it lives, even under a free name when two repos share a folder (N3).

    Raises:
        InvalidRepoError: ``repo`` isn't ``owner/name``.
    """
    owner, name = parse_repo(repo)
    return f"https://github.com/{owner}/{name}#how-it-works"


def review_title(number: int, title: str) -> str:
    """``#<n> <title>``, on one line and bounded."""
    clean = " ".join(title.split())[:TITLE_MAX] or "Untitled"
    return f"#{number} {clean}"


# --- the kept page ------------------------------------------------------------------------------


def _utc_stamp(at: datetime) -> str:
    """``at`` in UTC, like the provenance stamps: the page reads the same on any host."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str) -> str:
    return " ".join(inert(text).split())


def _code(text: str) -> str:
    """Inline code that its own text can't close."""
    return "`" + _one_line(text).replace("`", "'") + "`"


def review_front_matter(
    kept: KeptRound, existing: Mapping[str, object] | None = None
) -> dict[str, object]:
    """The page's ``jesinia:`` mapping: the PR's facts as of the newest kept round. Keys the
    author added are kept, and keeping an older round again never moves the facts back."""
    merged = dict(existing or {})
    newest = merged.get("round")
    if isinstance(newest, int) and not isinstance(newest, bool) and newest > kept.round_id:
        return merged
    merged.update(
        {
            "kind": REVIEW_KIND,
            "source": kept.url,
            "repo": kept.repo,
            "pr": kept.number,
            "round": kept.round_id,
            "head": kept.head_sha,
            "verdict": kept.verdict,
            "outcome": kept.outcome,
            "reviewed": _utc_stamp(kept.reviewed),
            "code": [{"repo": kept.repo, "path": path} for path in kept.paths[:CODE_MAX]],
        }
    )
    return merged


def findings_that_mattered(findings: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Critical and important findings, and every design finding (the system-level ones,
    such as a change your system notes contradict), most severe first, each once."""
    seen: list[Mapping[str, object]] = []
    for item in findings:
        severe = item.get("severity") in ("critical", "important")
        if (severe or item.get("group") == "design") and item not in seen:
            seen.append(item)
    seen.sort(key=lambda item: _SEVERITY_RANK.get(str(item.get("severity")), 3))
    return seen[:FINDINGS_MAX]


def _finding_line(item: Mapping[str, object]) -> str:
    where = f"{item.get('path', '?')}:{item.get('start_line', '?')}–{item.get('end_line', '?')}"
    label = f"{item.get('group', '?')} · {item.get('severity', '?')}"
    return f"- **{_one_line(label)}** {_code(where)}: {_one_line(str(item.get('explanation', '')))}"


def _open_marker(round_id: int) -> str:
    return f"<!-- jesinia:review-round {round_id} -->"


def _close_marker(round_id: int) -> str:
    return f"<!-- /jesinia:review-round {round_id} -->"


def round_section(kept: KeptRound, authors: Authors, tags: Sequence[str]) -> str:
    """The round's section of the page, between its markers."""
    day = kept.reviewed.astimezone(UTC).date().isoformat()
    outcome = _OUTCOME_WORDS.get(kept.outcome, _one_line(kept.outcome))
    verdict = _VERDICT_WORDS.get(kept.verdict or "", "none")
    facts = f"Head {_code(kept.head_sha[:12])} · Jesinia's verdict: {verdict}"
    if kept.review_url and _GITHUB_URL.fullmatch(kept.review_url):
        facts += f" · <{kept.review_url}>"
    parts = [_open_marker(kept.round_id), f"## Round {kept.round_id}: {outcome}, {day}", facts]
    if kept.overview.strip():
        parts += ["### Overview", authors.overview.wrap(kept.overview)]
    if kept.steps:
        listed = "\n".join(
            f"{n}. {_one_line(step)}" for n, step in enumerate(kept.steps[:STEPS_MAX], 1)
        )
        parts += ["### Walkthrough", authors.walkthrough.wrap(listed)]
    mattered = findings_that_mattered(kept.findings)
    if mattered:
        parts += [
            "### Findings that mattered",
            authors.findings.wrap("\n".join(_finding_line(item) for item in mattered)),
        ]
    notes = [inert(kept.summary.strip())] if kept.summary.strip() else []
    notes += [
        f"- {_code(c.path if c.line is None else f'{c.path}:{c.line}')}: {_one_line(c.body)}"
        for c in kept.comments[:COMMENTS_MAX]
    ]
    if notes:
        parts += ["### Your notes", "\n\n".join(notes)]
    if tags:
        parts.append(render_tag_line(tags))
    parts.append(_close_marker(kept.round_id))
    return "\n\n".join(parts)


_ANY_OPEN = re.compile(r"<!-- jesinia:review-round \d+ -->")


def merge_round_section(body: str, round_id: int, section: str) -> str:
    """``body`` with the round's section replaced, or appended. A pair counts only when the
    close marker comes before the next open marker of any round, and with more than one pair
    the last wins: a marker the author broke never swallows the text after it."""
    opening, close = _open_marker(round_id), _close_marker(round_id)
    pair: tuple[int, int] | None = None
    start = body.find(opening)
    while start != -1:
        end = body.find(close, start)
        following = _ANY_OPEN.search(body, start + len(opening))
        if end != -1 and (following is None or end < following.start()):
            pair = (start, end + len(close))
        start = body.find(opening, start + len(opening))
    if pair is not None:
        return body[: pair[0]] + section + body[pair[1] :]
    if not body.strip():
        return f"{section}\n"
    return f"{body.rstrip()}\n\n{section}\n"


# --- How it works -------------------------------------------------------------------------------


def system_seed(
    kept: KeptRound, background: Sequence[Mapping[str, object]], provenance: Provenance
) -> str:
    """A first ``How it works`` from a deep review: its Background sections (how the parts
    the PR touches work), else its overview; ``""`` when the review has neither."""
    sections = [
        f"## {_one_line(str(item['title']))}\n\n{str(item['body']).strip()}"
        for item in background
        if isinstance(item.get("title"), str) and isinstance(item.get("body"), str)
    ]
    if not sections and kept.overview.strip():
        sections = [f"## What it is for\n\n{kept.overview.strip()}"]
    if not sections:
        return ""
    intro = (
        f"How {kept.repo} works: its main parts and how they behave. Seeded from the review of "
        f"{_one_line(review_title(kept.number, kept.title))}; edit it freely. Jesinia changes "
        "this page only when you accept an update."
    )
    body = "\n\n".join(sections)
    return f"{intro}\n\n{provenance.wrap(body)}\n"


def system_update(current: str, kept: KeptRound, provenance: Provenance) -> str:
    """``current`` with what the approved PR changed appended (its purpose and the steps of
    its walkthrough); ``""`` when the review has neither."""
    items = [kept.overview.strip()] if kept.overview.strip() else []
    if kept.steps:
        items.append("\n".join(f"- {_one_line(step)}" for step in kept.steps[:STEPS_MAX]))
    if not items:
        return ""
    day = kept.reviewed.astimezone(UTC).date().isoformat()
    heading = f"## What {_one_line(review_title(kept.number, kept.title))} changed ({day})"
    text = provenance.wrap("\n\n".join(items))
    return (
        f"{current.rstrip()}\n\n{heading}\n\n{text}\n"
        if current.strip()
        else f"{heading}\n\n{text}\n"
    )
