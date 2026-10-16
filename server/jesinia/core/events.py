# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process publish/subscribe for module events (e.g. ``kb.page_changed``)."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    topic: str
    payload: Mapping[str, object] = field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Delivers each event to the handlers subscribed to its topic, in order.

    A failing handler is logged and does not stop delivery to the others or reach the
    publisher.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        handlers = self._subs.get(topic, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        for handler in list(self._subs.get(event.topic, ())):
            try:
                await handler(event)
            except Exception:
                log.exception("event handler failed for %s", event.topic)
