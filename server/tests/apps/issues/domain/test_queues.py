# SPDX-License-Identifier: AGPL-3.0-or-later
"""Work queue rules (spec §5.2; plan Task 9; rulings IR11, IR15): items and their local order,
priority bands, done, readiness warnings and the agent's queue proposals. Pure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jesinia.apps.issues.domain.epics import Blocker, Readiness
from jesinia.apps.issues.domain.queues import (
    NOTE_MAX,
    QUEUE_ITEMS_MAX,
    QUEUE_NAME_MAX,
    SUGGEST_NEXT_MAX,
    InvalidQueueError,
    ItemRef,
    QueueItem,
    QueueItemExistsError,
    QueueItemNotFoundError,
    QueueItemStateError,
    QueueReply,
    band_position,
    bands,
    done_items,
    insert,
    item_note,
    item_ref,
    mark_done,
    mark_open,
    move,
    next_draft,
    next_items,
    next_scope,
    open_items,
    order_draft,
    order_keys,
    order_scope,
    parse_ref,
    priority_order,
    queue_description,
    queue_name,
    remove,
    reorder,
    split_draft,
    split_moves,
    split_scope,
    warning_text,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
REPO = "acme/web"


def ref(n: int, kind: str = "issue", repo: str = REPO) -> ItemRef:
    return ItemRef(kind, repo, n)  # type: ignore[arg-type]


def item(n: int, kind: str = "issue", *, done: bool = False) -> QueueItem:
    return QueueItem(ref(n, kind), 0, "", T0, T0 if done else None)


def order(items: list[QueueItem]) -> list[int]:
    return [i.ref.n for i in items]


def reply(mode: str, *items: dict[str, object]) -> QueueReply:
    return QueueReply.from_json({"mode": mode, "items": list(items)})


# Items --------------------------------------------------------------------------------------


def test_an_item_ref_names_an_issue_epic_or_milestone() -> None:
    assert str(ref(3)) == "acme/web#3" and ref(3).ref == "acme/web#3"
    assert str(ref(2, "milestone")) == "milestone 2 of acme/web"
    # An issue and the same issue as an epic are one item; a milestone with that number isn't.
    assert ref(3).key == ref(3, "epic").key == ("issue", "acme/web", 3)
    assert ref(3, "milestone").key != ref(3).key
    assert ItemRef("issue", "Acme/Web", 3).key == ref(3).key
    assert parse_ref("epic", "acme/web#12") == ref(12, "epic")
    for bad in [("task", REPO, 1), ("issue", "nope", 1), ("issue", REPO, 0)]:
        with pytest.raises(InvalidQueueError):
            ItemRef(*bad)  # type: ignore[arg-type]
    for text in ["acme/web", "acme/web#x", "#3"]:
        with pytest.raises(InvalidQueueError):
            parse_ref("issue", text)


def test_names_descriptions_and_notes_are_bounded() -> None:
    assert queue_name("  Agents:   this week ") == "Agents: this week"
    assert queue_description("  launch work \n") == "launch work"
    assert item_note(" mine ") == "mine"
    for bad in ["", "   ", "x" * (QUEUE_NAME_MAX + 1)]:
        with pytest.raises(InvalidQueueError):
            queue_name(bad)
    with pytest.raises(InvalidQueueError):
        queue_description("x" * 1001)
    with pytest.raises(InvalidQueueError):
        item_note("x" * (NOTE_MAX + 1))


def test_order_is_positions_of_the_open_items() -> None:
    items = insert([], item(1), None)
    items = insert(items, item(2), None)
    items = insert(items, item(3), 0)
    assert order(items) == [3, 1, 2] and [i.position for i in items] == [0, 1, 2]
    items = insert(items, item(4), 99)  # past the end: last
    assert order(items) == [3, 1, 2, 4]
    items = move(items, ref(4).key, 1)
    assert order(items) == [3, 4, 1, 2]
    items = move(items, ref(3, "epic").key, 9)
    assert order(items) == [4, 1, 2, 3]
    assert order(remove(items, ref(1).key)) == [4, 2, 3]
    with pytest.raises(QueueItemExistsError):
        insert(items, item(1, "epic"), None)
    with pytest.raises(QueueItemNotFoundError):
        move(items, ref(9).key, 0)
    with pytest.raises(QueueItemNotFoundError):
        remove(items, ref(9).key)


def test_done_items_keep_their_place_for_undo() -> None:
    items = [item(1), item(2), item(3)]
    items = mark_done(items, ref(2).key, T0, "p1")
    assert order(open_items(items)) == [1, 3] and order(done_items(items)) == [2]
    assert done_items(items)[0].done_pending == "p1"
    # Positions count open items: index 1 is after #1 and #3, i.e. the end.
    items = insert(items, item(4), 1)
    assert order(open_items(items)) == [1, 4, 3]
    with pytest.raises(QueueItemStateError):
        mark_done(items, ref(2).key, T0, None)
    with pytest.raises(QueueItemStateError):
        move(items, ref(2).key, 0)
    with pytest.raises(QueueItemStateError):
        mark_open(items, ref(1).key)
    items = mark_open(items, ref(2).key)
    assert order(open_items(items)) == [1, 2, 4, 3]  # back where it was
    later = mark_done(mark_done(items, ref(1).key, T0, None), ref(3).key, T0 + timedelta(1), None)
    assert order(done_items(later)) == [3, 1]  # newest done first


def test_a_queue_holds_a_bounded_number_of_items() -> None:
    items = [QueueItem(ref(n), n, "", T0) for n in range(1, QUEUE_ITEMS_MAX + 1)]
    with pytest.raises(InvalidQueueError):
        insert(items, item(QUEUE_ITEMS_MAX + 1), None)


def test_reorder_puts_the_named_items_first() -> None:
    items = mark_done([item(1), item(2), item(3), item(4)], ref(3).key, T0, None)
    again = reorder(items, [ref(4).key, ref(9).key, ref(1).key, ref(4).key, ref(3).key])
    assert order(open_items(again)) == [4, 1, 2]
    assert order(done_items(again)) == [3]


# Bands --------------------------------------------------------------------------------------


def test_bands_group_open_items_by_priority_in_queue_order() -> None:
    items = mark_done([item(n) for n in range(1, 7)], ref(6).key, T0, None)
    priorities: dict[int, tuple[int, str] | None] = {
        1: (1, "priority: medium"),
        2: (0, "priority: high"),
        3: None,
        4: (0, "Priority: High"),
        5: (1, "priority: medium"),
        6: (0, "priority: high"),
    }
    found = bands(items, lambda i: priorities[i.ref.n])
    assert [(b.value, order(list(b.items))) for b in found] == [
        ("priority: high", [2, 4]),
        ("priority: medium", [1, 5]),
        (None, [3]),
    ]


def test_a_band_position_lands_the_item_among_that_bands_items() -> None:
    items = [item(n) for n in range(1, 6)]
    high = [ref(2).key, ref(4).key]
    # Into the high band (#2, #4) at its top: just before #2.
    assert order(move(items, ref(5).key, band_position(items, ref(5).key, high, 0))) == [
        1, 5, 2, 3, 4,
    ]  # fmt: skip
    # At its end (or no index): just after #4.
    for index in (None, 2, 7):
        moved = move(items, ref(1).key, band_position(items, ref(1).key, high, index))
        assert order(moved) == [2, 3, 4, 1, 5]
    # Within its own band: the item itself doesn't count.
    assert band_position(items, ref(4).key, high, 0) == 1
    # An empty band keeps the item where it is.
    assert band_position(items, ref(3).key, [], 0) == 2
    assert band_position(items, ref(3).key, [ref(3).key], None) == 2
    assert band_position(items, ref(9).key, [], 0) == 5  # not an open item: the end


# Warnings -----------------------------------------------------------------------------------


def test_a_warning_says_why_an_item_isnt_ready() -> None:
    assert warning_text(Readiness(True, (), False)) is None
    blocked = Readiness(False, (Blocker(REPO, 4, "missing priority"),), True)
    assert warning_text(blocked) == "acme/web#4: missing priority; won't fit one PR"
    assert warning_text(Readiness(False, (), False, "it is closed")) == "it is closed"
    assert warning_text(Readiness(False, (), False)) == "not ready"
    assert warning_text(Readiness(True, (), True)) == "won't fit one PR"


# Proposals ----------------------------------------------------------------------------------


def test_next_keeps_only_listed_candidates_once() -> None:
    candidates = {ref(2).key: ref(2), ref(3).key: ref(3, "epic"), ref(1, "milestone").key: ref(
        1, "milestone"
    )}  # fmt: skip
    found = next_draft(
        reply(
            "next",
            {"repo": "acme/web", "number": 3, "reason": "  unblocks\n launch "},
            {"repo": "ACME/web", "number": 3, "reason": "again"},
            {"repo": "acme/web", "number": 9, "reason": "not offered"},
            {"repo": "acme/web", "number": 2, "reason": "   "},
            {"repo": "acme/web", "number": 1, "kind": "milestone", "reason": "the launch"},
        ),
        candidates,
        "q1",
    )
    assert found is not None and found.kind == "queue_next"
    assert found.payload == {
        "queue": "q1",
        "items": [
            {"kind": "epic", "repo": "acme/web", "n": 3, "reason": "unblocks launch"},
            {"kind": "milestone", "repo": "acme/web", "n": 1, "reason": "the launch"},
        ],
    }
    assert next_items(found.payload) == [ref(3, "epic"), ref(1, "milestone")]
    assert (
        next_draft(reply("order", {"repo": REPO, "number": 2, "reason": "x"}), candidates, "q1")
        is None
    )
    assert next_draft(reply("next"), candidates, "q1") is None
    many = {ref(n).key: ref(n) for n in range(1, 40)}
    lots = [{"repo": REPO, "number": n, "reason": "r"} for n in range(1, 40)]
    capped = next_draft(reply("next", *lots), many, "q1")
    assert capped is not None and len(next_items(capped.payload)) == SUGGEST_NEXT_MAX


def test_order_proposes_a_full_order_of_the_open_items() -> None:
    current = [item(1), item(2), item(3, "epic"), item(4, "milestone")]
    found = order_draft(
        reply(
            "order",
            {"repo": REPO, "number": 3, "position": 2, "reason": "after its blocker"},
            {"repo": REPO, "number": 2, "position": 1, "reason": "blocks #3"},
            {"repo": REPO, "number": 4, "kind": "milestone", "reason": "last"},
            {"repo": REPO, "number": 2, "position": 5, "reason": "again"},
            {"repo": REPO, "number": 8, "position": 1, "reason": "not in the queue"},
        ),
        current,
        "q1",
    )
    assert found is not None and found.kind == "queue_order"
    keys = order_keys(found.payload)
    assert keys == [ref(2).key, ref(3).key, ref(4, "milestone").key, ref(1).key]
    items = found.payload["items"]
    assert isinstance(items, list) and items[-1]["reason"] == "not placed by the agent; kept last"
    # Proposing the current order, a wrong mode or nothing usable: no draft.
    same: list[dict[str, object]] = [
        {"repo": REPO, "number": n, "position": n, "reason": "r"} for n in (1, 2, 3)
    ]
    assert order_draft(reply("order", *same), current[:3], "q1") is None
    assert order_draft(reply("next", *same), current, "q1") is None
    assert order_draft(reply("order"), current, "q1") is None


def test_split_moves_items_between_the_queues_asked() -> None:
    placed = {ref(1).key: (ref(1), "qa"), ref(2).key: (ref(2), "qa"), ref(3).key: (ref(3), "qb")}
    names = {"mine": "qa", "agents: this week": "qb"}
    found = split_draft(
        reply(
            "split",
            {"repo": REPO, "number": 1, "queue": " Agents:  this week ", "reason": "agent-ready"},
            {"repo": REPO, "number": 2, "queue": "   ", "reason": "a blank queue"},
            {"repo": REPO, "number": 2, "queue": "Mine", "reason": "stays"},
            {"repo": REPO, "number": 3, "queue": "Launch", "reason": "no such queue"},
            {"repo": REPO, "number": 3, "reason": "no queue named"},
            {"repo": REPO, "number": 9, "queue": "Mine", "reason": "not queued"},
            {"repo": REPO, "number": 1, "queue": "Mine", "reason": "again"},
        ),
        placed,
        names,
        ["qb", "qa"],
    )
    assert found is not None and found.kind == "queue_split"
    assert found.payload["queues"] == ["qa", "qb"]
    moves = split_moves(found.payload)
    assert [(m.ref, m.source, m.target, m.reason) for m in moves] == [
        (ref(1), "qa", "qb", "agent-ready")
    ]
    assert split_draft(reply("split"), placed, names, ["qa", "qb"]) is None
    assert (
        split_draft(
            reply("next", {"repo": REPO, "number": 1, "queue": "Mine", "reason": "r"}),
            placed,
            names,
            ["qa", "qb"],
        )
        is None
    )


def test_scopes_name_the_queues_asked_about() -> None:
    assert next_scope("q1") == "queue_next:q1"
    assert order_scope("q1") == "queue_order:q1"
    assert split_scope(["qb", "qa", "qb"]) == "queue_split:qa,qb"


def test_a_priority_drafts_order_is_its_items_by_rank() -> None:
    payload = {
        "items": [
            {"repo": REPO, "n": 5, "priority": None, "current": None, "rank": 2, "reason": "r"},
            {"repo": REPO, "n": 3, "priority": "priority: high", "current": None, "rank": 1,
             "reason": "r"},
        ]
    }  # fmt: skip
    assert priority_order(payload) == [(REPO, 3), (REPO, 5)]


def test_item_ref_checks_the_kind_text() -> None:
    assert item_ref("epic", REPO, 3) == ref(3, "epic")
    with pytest.raises(InvalidQueueError):
        item_ref("task", REPO, 3)
