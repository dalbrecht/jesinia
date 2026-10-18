# SPDX-License-Identifier: AGPL-3.0-or-later
"""Triage state (spec §3, §6.2): the Inbox rule, the views, saved filters and the sync cursor.

Pure: no I/O.

- **Inbox:** an issue opened after its repo was added, plus any issue whose ``complete`` flips
  from true to false on GitHub. Comments and body edits don't change labels, so they never send
  an issue back. The flag means "untriaged since it arrived or regressed": it is cleared as soon
  as the issue is complete (on GitHub or by an approval), so a later schema change can't bring
  old issues back (ruling IR30). The Inbox *view* is ``inbox AND NOT complete`` (IR9).
- **Backlog:** every other open issue. A newly added repo's existing issues land here.
- **Sync cursor:** GitHub lists issues by page number. Paging forward while issues move (an
  edit sends one to the end of an ``updated``-ascending list) shifts every later page and skips
  an issue, so the cursor restarts at page 1 with ``since`` = the high-water ``updated_at``
  whenever a page moved it forward. ``since`` is inclusive, so the issues at that exact time come
  back; sync skips one only when its content is unchanged, so an edit within the same second as
  the mark is still taken.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, get_args

from jesinia.apps.issues.domain.completeness import completeness
from jesinia.apps.issues.domain.schema import LabelSchema, classify, label_key
from jesinia.services.github.domain.repo import validate_repo

ViewName = Literal["inbox", "backlog"]
# Saved filters (spec §3.2, ruling IR10): ``incomplete``; ``stale_90d`` (no activity in 90
# days); ``human_needed_could_be_ready`` (``human-needed`` with an open suggestion that it could
# be ``agent-ready``); ``possible_duplicates`` (an open duplicate suggestion).
SavedFilter = Literal[
    "incomplete", "stale_90d", "human_needed_could_be_ready", "possible_duplicates"
]
SAVED_FILTERS: tuple[SavedFilter, ...] = get_args(SavedFilter)
_VIEWS: tuple[ViewName, ...] = get_args(ViewName)
STALE_AFTER = timedelta(days=90)


class TriageDomainError(ValueError):
    """Base for invalid triage requests."""


class InvalidViewError(TriageDomainError):
    """Not ``inbox`` or ``backlog``."""


class InvalidFilterError(TriageDomainError):
    """Not a known saved filter."""


class RepoNotWatchedError(TriageDomainError):
    """The repo was never added (or was removed): Jesinia only syncs repos the author added."""


class RepoMovedError(TriageDomainError):
    """GitHub lists a watched repo's issues under another name (it was renamed or
    transferred): the sync records it rather than mixing two names in the cache."""


class IssueNotCachedError(TriageDomainError):
    """The issue isn't in the cache (its repo hasn't synced it yet)."""


def parse_view(value: str) -> ViewName:
    for view in _VIEWS:
        if value == view:
            return view
    raise InvalidViewError(f"view must be one of {', '.join(_VIEWS)}, got {value!r}")


def parse_saved_filter(value: str | None) -> SavedFilter | None:
    if value is None:
        return None
    for saved in SAVED_FILTERS:
        if value == saved:
            return saved
    raise InvalidFilterError(f"saved filter must be one of {', '.join(SAVED_FILTERS)}")


@dataclass(frozen=True)
class IssueFilters:
    """What a list shows: one repo (or all watched) and one saved filter (or none)."""

    repo: str | None = None
    saved: SavedFilter | None = None

    def __post_init__(self) -> None:
        if self.repo is not None:
            validate_repo(self.repo)
        parse_saved_filter(self.saved)


def content_hash(title: str, body: str, labels: Iterable[str]) -> str:
    """A digest of what an issue *says*: its title, body and labels (compared as label names
    compare). GitHub bumps ``updated_at`` for comments, assignees and milestones too; only a
    change of this digest is an edit worth re-suggesting for."""
    names = sorted({label_key(label) for label in labels})
    text = json.dumps([title, body, names], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreviousTriage:
    """What the cache knew about an issue before this sync; ``content`` is its
    ``content_hash``."""

    complete: bool
    inbox: bool
    is_open: bool = True
    content: str | None = None


def is_inbox(
    previous: PreviousTriage | None, *, created_at: datetime, added_at: datetime, complete: bool
) -> bool:
    """The issue's Inbox flag after a sync.

    A complete issue is triaged, so never in the Inbox. A first sighting is in the Inbox only
    when opened after the repo was added (the same second counts, at GitHub's precision);
    after that an incomplete issue stays there, or enters it when it was complete before.
    """
    if complete:
        return False
    if previous is None:
        # GitHub's times have whole seconds and the watch time has microseconds: an issue
        # opened in the same second as the watch counts as new.
        return created_at >= added_at.replace(microsecond=0)
    return previous.inbox or previous.complete


def in_view(view: ViewName, *, inbox: bool, complete: bool) -> bool:
    """Whether an open issue shows in ``view``; the store's queries mirror this."""
    in_inbox = inbox and not complete
    return in_inbox if view == "inbox" else not in_inbox


def stale_before(now: datetime) -> datetime:
    """``stale_90d``: open issues not updated since this moment."""
    return now - STALE_AFTER


@dataclass(frozen=True)
class Triage:
    """An issue's completeness against its repo's schema, as stored with it."""

    complete: bool
    missing: list[str]
    conflicts: list[str]
    is_epic: bool
    readiness: str | None
    priority: str | None
    type: str | None
    areas: list[str]


def triage(labels: Iterable[str], sub_issue_count: int, schema: LabelSchema) -> Triage:
    """Classify ``labels`` and check completeness; sub-issues (or the epic label) make an
    epic (spec §6.2)."""
    classified = classify(labels, schema)
    has_children = sub_issue_count > 0
    result = completeness(classified, is_epic=has_children)
    return Triage(
        complete=result.complete,
        missing=result.missing,
        conflicts=result.conflicts,
        is_epic=has_children or classified.is_epic,
        readiness=classified.readiness,
        priority=classified.priority,
        type=classified.type,
        areas=classified.areas,
    )


@dataclass(frozen=True)
class SyncCursor:
    """Where a repo's incremental sync stands; persisted after every page.

    ``since`` and ``page_token`` are the next request. ``high_water`` is the latest
    ``updated_at`` taken and ``seen_ids`` the issues (database ids) taken at exactly that time.
    """

    since: datetime | None = None
    page_token: str | None = None
    high_water: datetime | None = None
    seen_ids: frozenset[int] = field(default_factory=frozenset)

    def covers(self, updated_at: datetime, issue_id: int) -> bool:
        """Whether an issue at this ``updated_at`` was already taken. At exactly the mark a
        second edit can share the time, so the caller re-takes a covered issue whose content
        changed."""
        if self.high_water is None:
            return False
        if updated_at == self.high_water:
            return issue_id in self.seen_ids
        return updated_at < self.high_water

    def after_page(
        self, taken: Sequence[tuple[datetime, int]], next_token: str | None
    ) -> tuple[SyncCursor, bool]:
        """The cursor after a page whose issues were ``taken`` (``(updated_at, id)``,
        ascending), and whether the sync is finished.

        When the page moved the high-water mark forward, the next request restarts at page 1
        from it, so issues moving mid-sync can't shift a page out from under the sync. Only a
        page that couldn't move it (every issue at one ``updated_at``, or no issues at all)
        pages forward.
        """
        high, seen = self.high_water, set(self.seen_ids)
        for updated_at, issue_id in taken:
            if high is None or updated_at > high:
                high, seen = updated_at, {issue_id}
            elif updated_at == high:
                seen.add(issue_id)
        ids = frozenset(seen)
        if next_token is None:
            return SyncCursor(high, None, high, ids), True
        moved = high is not None and (self.since is None or high > self.since)
        if moved:
            return SyncCursor(high, None, high, ids), False
        return SyncCursor(self.since, next_token, high, ids), False

    def to_json(self) -> str:
        return json.dumps(
            {
                "since": None if self.since is None else self.since.isoformat(),
                "page_token": self.page_token,
                "high_water": None if self.high_water is None else self.high_water.isoformat(),
                "seen_ids": sorted(self.seen_ids),
            }
        )

    @classmethod
    def from_json(cls, text: str) -> SyncCursor:
        data = json.loads(text)
        since, high = data["since"], data["high_water"]
        return cls(
            since=None if since is None else datetime.fromisoformat(since),
            page_token=data["page_token"],
            high_water=None if high is None else datetime.fromisoformat(high),
            seen_ids=frozenset(int(i) for i in data["seen_ids"]),
        )


@dataclass(frozen=True)
class IssueRow:
    """One issue in the Inbox or Backlog list. ``title`` is untrusted text, shown as data."""

    repo: str
    number: int
    title: str
    url: str
    author: str
    labels: list[str]
    readiness: str | None
    priority: str | None
    type: str | None
    areas: list[str]
    complete: bool
    missing: list[str]
    conflicts: list[str]
    is_epic: bool
    sub_issue_count: int
    parent: str | None  # "owner/name#n"
    milestone: str | None
    created_at: datetime
    updated_at: datetime
    # GitHub's ``open`` or ``closed``, and the linked PRs' summary (``merged``, ``open``,
    # ``closed`` or ``None``): the board's status view is derived from them (spec §6.2).
    state: str
    linked_pr_state: str | None


@dataclass(frozen=True)
class IssueView:
    """A list served from the cache, with the sync's state reported, never awaited.

    ``synced_at`` is the oldest last good sync among the listed repos (``None`` while any has
    never synced); ``sync_error`` joins each listed repo's last failure.
    """

    rows: list[IssueRow]
    synced_at: datetime | None
    syncing: bool
    sync_error: str | None
