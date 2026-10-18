# SPDX-License-Identifier: AGPL-3.0-or-later
"""Work queues (spec §5.2; plan Task 9; rulings IR11, IR15). Pure: no I/O.

A work queue is a named, ordered list of issues, epics and milestones, kept in Jesinia only.
Its order is local and applies at once (IR15); only Done and a move to another priority band
stage GitHub changes, and that is the application's business (``application/queues.py``).

- **Items** are named by ``ItemRef``: an issue or an epic is ``owner/repo#n`` (one item either
  way: an issue that becomes an epic stays the same item), a milestone is its number in a repo.
- **Order:** an item's position counts only the open items, as the author sees them. A done item
  keeps its place among the others, so Undo puts it back where it was.
- **Bands:** the open items grouped by priority, each band in queue order.
- **Proposals:** the agent's replies (``schemas/queue.json``) become drafts of the kinds
  ``queue_next``, ``queue_order`` and ``queue_split`` (IR11). Jesinia decides what a reply may
  propose: only items it listed, each once, and only queues it asked about. Approving one only
  ever changes the local queues.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast, get_args

from jesinia.apps.issues.domain.epics import CROSS_REPO_WARNING, ItemKind, Readiness
from jesinia.apps.issues.domain.schema import label_key
from jesinia.apps.issues.domain.suggestions import Draft, one_line
from jesinia.services.github.domain.repo import InvalidRepoError, validate_repo

ITEM_KINDS: tuple[ItemKind, ...] = get_args(ItemKind)
DoneThen = Literal["close", "agent_ready"]
DONE_THENS: tuple[DoneThen, ...] = get_args(DoneThen)
QueueDraftKind = Literal["queue_next", "queue_order", "queue_split"]
QUEUE_DRAFT_KINDS: tuple[QueueDraftKind, ...] = get_args(QueueDraftKind)

QUEUE_NAME_MAX = 100
QUEUE_DESCRIPTION_MAX = 1000
NOTE_MAX = 500
QUEUES_MAX = 50
QUEUE_ITEMS_MAX = 500
SUGGEST_NEXT_MAX = 20
KEPT_LAST = "not placed by the agent; kept last"
NOT_READY = "not ready"

# An item's identity: an issue and the same issue as an epic are one item.
ItemKey = tuple[str, str, int]


class QueueDomainError(ValueError):
    """Base of the queue rules' errors."""


class InvalidQueueError(QueueDomainError):
    """A queue, item or request the rules refuse (a blank name, an unknown kind, ...)."""


class QueueNotFoundError(QueueDomainError):
    """No such queue."""


class QueueNameTakenError(QueueDomainError):
    """Another queue has that name (names compare without case)."""


class QueueItemNotFoundError(QueueDomainError):
    """The item isn't in the queue."""


class QueueItemExistsError(QueueDomainError):
    """The item is in the queue already."""


class QueueItemStateError(QueueDomainError):
    """The item is done already, or (for Undo) isn't done."""


class QueueItemNotWritableError(InvalidQueueError):
    """Done or a band move would change GitHub, and the item is a milestone, which Jesinia never
    writes."""


# Items --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemRef:
    """A queue item: an issue, an epic, or a milestone (``n`` is its number) of ``repo``.

    Raises:
        InvalidQueueError: an unknown kind, a repo that isn't ``owner/name``, or ``n`` < 1.
    """

    kind: ItemKind
    repo: str
    n: int

    def __post_init__(self) -> None:
        if self.kind not in ITEM_KINDS:
            raise InvalidQueueError(f"an item is one of {', '.join(ITEM_KINDS)}")
        try:
            validate_repo(self.repo)
        except InvalidRepoError as exc:
            raise InvalidQueueError(str(exc)) from exc
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 1:
            raise InvalidQueueError("an item's number is a positive number")

    @property
    def key(self) -> ItemKey:
        group = "milestone" if self.kind == "milestone" else "issue"
        return (group, self.repo.casefold(), self.n)

    @property
    def ref(self) -> str:
        """How the item is stored: ``owner/repo#n`` (with its kind beside it)."""
        return f"{self.repo}#{self.n}"

    def __str__(self) -> str:
        if self.kind == "milestone":
            return f"milestone {self.n} of {self.repo}"
        return self.ref


def parse_ref(kind: str, text: str) -> ItemRef:
    """The item stored as ``text`` (``owner/repo#n``) of ``kind``.

    Raises:
        InvalidQueueError: not ``owner/repo#n``.
    """
    repo, sep, number = text.rpartition("#")
    if not sep or not number.isdigit():
        raise InvalidQueueError(f"{text!r} isn't owner/repo#n")
    return ItemRef(cast(ItemKind, kind), repo, int(number))


@dataclass(frozen=True)
class QueueItem:
    """An item in a queue: its place (``position``), the author's ``note``, when it was added,
    and, once done, when (``done_at``) and the pending change Done staged (``done_pending``;
    ``None`` when its issue closed on GitHub)."""

    ref: ItemRef
    position: int
    note: str
    added_at: datetime
    done_at: datetime | None = None
    done_pending: str | None = None

    @property
    def key(self) -> ItemKey:
        return self.ref.key

    @property
    def is_done(self) -> bool:
        return self.done_at is not None


@dataclass(frozen=True)
class Queue:
    """A named queue with an optional description, in the author's order of queues."""

    id: str
    name: str
    description: str
    position: int
    created_at: datetime


def queue_name(value: str) -> str:
    """A queue's name on one line, at most ``QUEUE_NAME_MAX`` characters.

    Raises:
        InvalidQueueError: blank or too long.
    """
    name = " ".join(value.split())
    if not name or len(name) > QUEUE_NAME_MAX:
        raise InvalidQueueError(f"a queue's name is 1 to {QUEUE_NAME_MAX} characters")
    return name


def _bounded(value: str, limit: int, what: str) -> str:
    text = value.strip()
    if len(text) > limit:
        raise InvalidQueueError(f"{what} is at most {limit} characters")
    return text


def queue_description(value: str) -> str:
    """Raises: InvalidQueueError (too long)."""
    return _bounded(value, QUEUE_DESCRIPTION_MAX, "a queue's description")


def item_note(value: str) -> str:
    """Raises: InvalidQueueError (too long)."""
    return _bounded(value, NOTE_MAX, "an item's note")


# Order --------------------------------------------------------------------------------------


def find(items: Sequence[QueueItem], key: ItemKey) -> QueueItem | None:
    return next((i for i in items if i.key == key), None)


def _found(items: Sequence[QueueItem], key: ItemKey) -> QueueItem:
    found = find(items, key)
    if found is None:
        raise QueueItemNotFoundError("that item isn't in the queue")
    return found


def open_items(items: Sequence[QueueItem]) -> list[QueueItem]:
    """The open items, in queue order."""
    return [i for i in sorted(items, key=lambda i: i.position) if not i.is_done]


def done_items(items: Sequence[QueueItem]) -> list[QueueItem]:
    """The done items, most recently done first."""
    done = [i for i in items if i.is_done]
    return sorted(done, key=lambda i: (cast(datetime, i.done_at), i.position), reverse=True)


def _numbered(items: Iterable[QueueItem]) -> list[QueueItem]:
    return [replace(item, position=index) for index, item in enumerate(items)]


def _with_open(items: Sequence[QueueItem], wanted: Sequence[QueueItem]) -> list[QueueItem]:
    """``items`` with their open items in the order ``wanted`` (at least as many as ``items``
    has open): each open slot takes the next wanted item, so done items keep their places;
    extra wanted items go at the end."""
    pending = list(wanted)
    rebuilt: list[QueueItem] = []
    for item in sorted(items, key=lambda i: i.position):
        rebuilt.append(item if item.is_done else pending.pop(0))
    return _numbered([*rebuilt, *pending])


def _at(sequence: list[QueueItem], item: QueueItem, position: int | None) -> list[QueueItem]:
    index = len(sequence) if position is None else max(0, min(position, len(sequence)))
    return [*sequence[:index], item, *sequence[index:]]


def insert(items: Sequence[QueueItem], item: QueueItem, position: int | None) -> list[QueueItem]:
    """``items`` with ``item`` at ``position`` among the open items (the end when ``None`` or
    past it).

    Raises:
        QueueItemExistsError: it is in the queue already.
        InvalidQueueError: the queue holds ``QUEUE_ITEMS_MAX`` items.
    """
    if find(items, item.key) is not None:
        raise QueueItemExistsError(f"{item.ref} is in the queue already")
    if len(items) >= QUEUE_ITEMS_MAX:
        raise InvalidQueueError(f"a queue holds at most {QUEUE_ITEMS_MAX} items")
    return _with_open(items, _at(open_items(items), item, position))


def move(items: Sequence[QueueItem], key: ItemKey, position: int) -> list[QueueItem]:
    """``items`` with the open item ``key`` moved to ``position`` among the other open items.

    Raises:
        QueueItemNotFoundError, QueueItemStateError (it is done).
    """
    item = _found(items, key)
    if item.is_done:
        raise QueueItemStateError(f"{item.ref} is done; undo it first")
    others = [i for i in open_items(items) if i.key != key]
    return _with_open([i for i in items if i.key != key], _at(others, item, position))


def remove(items: Sequence[QueueItem], key: ItemKey) -> list[QueueItem]:
    """Raises: QueueItemNotFoundError."""
    _found(items, key)
    return _numbered(i for i in sorted(items, key=lambda i: i.position) if i.key != key)


def mark_done(
    items: Sequence[QueueItem], key: ItemKey, now: datetime, pending: str | None
) -> list[QueueItem]:
    """``items`` with ``key`` done at ``now`` (``pending`` is the change Done staged).

    Raises:
        QueueItemNotFoundError, QueueItemStateError (done already).
    """
    item = _found(items, key)
    if item.is_done:
        raise QueueItemStateError(f"{item.ref} is done already")
    done = replace(item, done_at=now, done_pending=pending)
    return [done if i.key == key else i for i in items]


def mark_open(items: Sequence[QueueItem], key: ItemKey) -> list[QueueItem]:
    """``items`` with the done item ``key`` open again, in its old place (Undo).

    Raises:
        QueueItemNotFoundError, QueueItemStateError (it isn't done).
    """
    item = _found(items, key)
    if not item.is_done:
        raise QueueItemStateError(f"{item.ref} isn't done")
    reopened = replace(item, done_at=None, done_pending=None)
    return [reopened if i.key == key else i for i in items]


def reorder(items: Sequence[QueueItem], keys: Sequence[ItemKey]) -> list[QueueItem]:
    """``items`` with the open items named by ``keys`` first, in that order, and the other open
    items after them in their order. Keys of done or missing items are ignored."""
    current = open_items(items)
    by_key = {i.key: i for i in current}
    first = [by_key[k] for k in dict.fromkeys(keys) if k in by_key]
    rest = [i for i in current if i not in first]
    return _with_open(items, [*first, *rest])


# Bands --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    """The open items of one priority (``None``: without one), in queue order."""

    value: str | None
    items: tuple[QueueItem, ...]


# An item's priority: its rank in its repo's schema and the label, or ``None`` for none.
PriorityOf = Callable[[QueueItem], tuple[int, str] | None]


def bands(items: Sequence[QueueItem], priority: PriorityOf) -> list[Band]:
    """The open items grouped by priority (names compare as labels do), the highest band first
    (by rank in the schema) and the items without a priority last."""
    groups: dict[str | None, tuple[tuple[int, str], str | None, list[QueueItem]]] = {}
    for item in open_items(items):
        found = priority(item)
        key = None if found is None else label_key(found[1])
        if key not in groups:
            order = (1, "") if found is None else (0, f"{found[0]:09d}{key}")
            groups[key] = (order, None if found is None else found[1], [])
        groups[key][2].append(item)
    ordered = sorted(groups.values(), key=lambda g: g[0])
    return [Band(value, tuple(members)) for _, value, members in ordered]


def band_position(
    items: Sequence[QueueItem], key: ItemKey, members: Sequence[ItemKey], index: int | None
) -> int:
    """The position (for ``move``) that lands ``key`` at ``index`` among a band's ``members``
    (in queue order): just before the member at ``index``, or just after the last when
    ``index`` is ``None`` or past the end. In an empty band (the item aside), it stays put."""
    others = [i.key for i in open_items(items) if i.key != key]
    band = [m for m in members if m != key and m in others]
    if not band:
        current = [i.key for i in open_items(items)]
        return current.index(key) if key in current else len(others)
    if index is None or index >= len(band):
        return others.index(band[-1]) + 1
    return others.index(band[max(0, index)])


# Warnings -----------------------------------------------------------------------------------


def warning_text(readiness: Readiness) -> str | None:
    """Why an item isn't ready for an agent (spec §5.2: warned, never hidden), or ``None``."""
    parts = [] if readiness.note is None else [readiness.note]
    parts += [f"{b.repo}#{b.n}: {b.reason}" for b in readiness.blockers]
    if readiness.cross_repo:
        parts.append(CROSS_REPO_WARNING)
    if readiness.ready and not parts:
        return None
    return "; ".join(parts) or NOT_READY


# The agent's proposals ----------------------------------------------------------------------


@dataclass(frozen=True)
class QueueProposal:
    repo: str
    number: int
    reason: str
    position: int | None
    queue: str | None
    kind: str | None

    @property
    def key(self) -> ItemKey:
        group = "milestone" if self.kind == "milestone" else "issue"
        return (group, self.repo.casefold(), self.number)


@dataclass(frozen=True)
class QueueReply:
    """A queue reply already valid against ``schemas/queue.json``."""

    mode: str
    items: tuple[QueueProposal, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> QueueReply:
        items = cast(list[dict[str, object]], data["items"])
        return cls(
            str(data["mode"]),
            tuple(
                QueueProposal(
                    str(i["repo"]),
                    cast(int, i["number"]),
                    str(i["reason"]),
                    cast(int | None, i.get("position")),
                    cast(str | None, i.get("queue")),
                    cast(str | None, i.get("kind")),
                )
                for i in items
            ),
        )


def _usable(reply: QueueReply, mode: str) -> list[QueueProposal]:
    """The reply's proposals with a reason, each item once; none for another mode."""
    if reply.mode != mode:
        return []
    seen: set[ItemKey] = set()
    kept: list[QueueProposal] = []
    for proposal in reply.items:
        if proposal.key in seen or not one_line(proposal.reason):
            continue
        seen.add(proposal.key)
        kept.append(proposal)
    return kept


def _item_json(ref: ItemRef, reason: str) -> dict[str, object]:
    return {"kind": ref.kind, "repo": ref.repo, "n": ref.n, "reason": one_line(reason)}


def _ref_of(data: Mapping[str, object]) -> ItemRef:
    return ItemRef(cast(ItemKind, data["kind"]), str(data["repo"]), cast(int, data["n"]))


def next_scope(queue_id: str) -> str:
    return f"queue_next:{queue_id}"


def order_scope(queue_id: str) -> str:
    return f"queue_order:{queue_id}"


def split_scope(queue_ids: Sequence[str]) -> str:
    return "queue_split:" + ",".join(sorted(set(queue_ids)))


def next_draft(
    reply: QueueReply, candidates: Mapping[ItemKey, ItemRef], queue_id: str
) -> Draft | None:
    """The ``queue_next`` draft ``reply`` may produce: items to add to the queue, only among the
    ``candidates`` Jesinia listed, each once, at most ``SUGGEST_NEXT_MAX``. ``None`` when
    nothing is left. The payload is ``{"queue", "items": [{kind, repo, n, reason}]}``."""
    items = [
        _item_json(candidates[p.key], p.reason)
        for p in _usable(reply, "next")
        if p.key in candidates
    ][:SUGGEST_NEXT_MAX]
    if not items:
        return None
    return Draft("queue_next", {"queue": queue_id, "items": items}, f"Add {len(items)} items")


def next_items(payload: Mapping[str, object]) -> list[ItemRef]:
    return [_ref_of(i) for i in cast(list[dict[str, object]], payload["items"])]


def order_draft(reply: QueueReply, current: Sequence[QueueItem], queue_id: str) -> Draft | None:
    """The ``queue_order`` draft ``reply`` may produce for the queue's open items (``current``,
    in order): the items it placed, by position (unplaced ones after, in reply order), then the
    ones it left out, kept last in their order. ``None`` when that is the order already. The
    payload is ``{"queue", "items": [{kind, repo, n, reason}]}``, the whole order."""
    by_key = {i.key: i for i in current}
    placed = [p for p in _usable(reply, "order") if p.key in by_key]
    ranked = sorted(
        enumerate(placed),
        key=lambda pair: (pair[1].position is None, pair[1].position or 0, pair[0]),
    )
    proposed = [(by_key[p.key].ref, p.reason) for _, p in ranked]
    named = {ref.key for ref, _ in proposed}
    proposed += [(i.ref, KEPT_LAST) for i in current if i.key not in named]
    if [ref.key for ref, _ in proposed] == [i.key for i in current]:
        return None
    items = [_item_json(ref, reason) for ref, reason in proposed]
    return Draft("queue_order", {"queue": queue_id, "items": items}, "A new order for the queue")


def order_keys(payload: Mapping[str, object]) -> list[ItemKey]:
    return [ref.key for ref in next_items(payload)]


@dataclass(frozen=True)
class QueueMove:
    """A split proposal: move ``ref`` from the queue ``source`` to ``target``."""

    ref: ItemRef
    source: str
    target: str
    reason: str


def _queue_key(name: str) -> str | None:
    try:
        return queue_name(name).casefold()
    except InvalidQueueError:
        return None


def split_draft(
    reply: QueueReply,
    placed: Mapping[ItemKey, tuple[ItemRef, str]],
    names: Mapping[str, str],
    queue_ids: Sequence[str],
) -> Draft | None:
    """The ``queue_split`` draft ``reply`` may produce: moves of the open items Jesinia listed
    (``placed``: each with the queue it is in) to another of the queues asked about (``names``:
    each queue's name, casefolded, to its id). An item kept where it is, or sent to a queue
    nobody asked about, is dropped. ``None`` when no move is left. The payload is
    ``{"queues", "items": [{kind, repo, n, from, to, reason}]}``."""
    items: list[dict[str, object]] = []
    for proposal in _usable(reply, "split"):
        found = placed.get(proposal.key)
        target = None if proposal.queue is None else names.get(_queue_key(proposal.queue) or "")
        if found is None or target is None or target == found[1]:
            continue
        ref, source = found
        items.append({**_item_json(ref, proposal.reason), "from": source, "to": target})
    if not items:
        return None
    payload: dict[str, object] = {"queues": sorted(set(queue_ids)), "items": items}
    return Draft("queue_split", payload, f"Move {len(items)} items between queues")


def split_moves(payload: Mapping[str, object]) -> list[QueueMove]:
    return [
        QueueMove(_ref_of(i), str(i["from"]), str(i["to"]), str(i["reason"]))
        for i in cast(list[dict[str, object]], payload["items"])
    ]


def priority_order(payload: Mapping[str, object]) -> list[tuple[str, int]]:
    """A priority draft's issues (Task 7) in its order: by rank."""
    items = sorted(
        cast(list[dict[str, object]], payload["items"]), key=lambda i: cast(int, i["rank"])
    )
    return [(str(i["repo"]), cast(int, i["n"])) for i in items]
