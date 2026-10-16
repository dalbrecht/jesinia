# SPDX-License-Identifier: AGPL-3.0-or-later
import logging

import pytest

from jesinia.core.events import Event, EventBus


async def test_publish_reaches_topic_subscribers_only() -> None:
    bus = EventBus()
    got: list[str] = []

    async def on_a(e: Event) -> None:
        got.append(f"a:{e.payload['n']}")

    async def on_b(e: Event) -> None:
        got.append("b")

    bus.subscribe("a", on_a)
    bus.subscribe("b", on_b)
    await bus.publish(Event("a", {"n": 1}))
    assert got == ["a:1"]


async def test_handler_exception_is_logged_not_propagated(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    got: list[int] = []

    async def boom(e: Event) -> None:
        raise RuntimeError("nope")

    async def ok(e: Event) -> None:
        got.append(1)

    bus.subscribe("t", boom)
    bus.subscribe("t", ok)
    with caplog.at_level(logging.ERROR):
        await bus.publish(Event("t", {}))
    assert got == [1] and "nope" in caplog.text


async def test_unsubscribe() -> None:
    bus = EventBus()
    got: list[int] = []

    async def h(e: Event) -> None:
        got.append(1)

    bus.subscribe("t", h)
    bus.unsubscribe("t", h)
    bus.unsubscribe("t", h)  # idempotent
    await bus.publish(Event("t", {}))
    assert got == []
