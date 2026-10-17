# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/events``: selected bus topics as server-sent events, for the PWAs.

Only allowlisted topics are streamed. Each event gets a sequence id; a reconnecting
client passes ``after`` (or ``Last-Event-ID``) and receives what it missed from a
bounded in-memory buffer, then live events. A listener that falls a full buffer behind
is cut off rather than queued without limit; its client reconnects with
``Last-Event-ID`` and replays what the buffer still holds. Streams end once their
credentials stop being valid (token rotation signs browsers out).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from jesinia.core.auth import is_authorized, require_owner
from jesinia.core.events import Event, EventBus

STREAMED_TOPICS = frozenset({"kbsync.status", "kb.page_changed", "rss.post_changed"})
BUFFER = 500
KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class Logged:
    id: int
    topic: str
    payload: object

    def frame(self) -> bytes:
        data = json.dumps(self.payload, default=str)
        return f"id: {self.id}\nevent: {self.topic}\ndata: {data}\n\n".encode()


class EventLog:
    """Records streamed topics from the bus and fans them out to listeners."""

    def __init__(self, bus: EventBus, topics: frozenset[str] = STREAMED_TOPICS) -> None:
        self.topics = topics
        self._seq = 0
        self._buffer: deque[Logged] = deque(maxlen=BUFFER)
        self._listeners: set[asyncio.Queue[Logged]] = set()
        self._overflowed: set[asyncio.Queue[Logged]] = set()
        for topic in topics:
            bus.subscribe(topic, self._record)

    async def _record(self, event: Event) -> None:
        self._seq += 1
        item = Logged(self._seq, event.topic, dict(event.payload))
        self._buffer.append(item)
        for q in self._listeners - self._overflowed:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                self._overflowed.add(q)  # a stalled client; its stream ends, it reconnects

    def since(self, after: int, topics: set[str]) -> list[Logged]:
        return [e for e in self._buffer if e.id > after and e.topic in topics]

    def listen(self) -> asyncio.Queue[Logged]:
        q: asyncio.Queue[Logged] = asyncio.Queue(maxsize=BUFFER)
        self._listeners.add(q)
        return q

    def overflowed(self, q: asyncio.Queue[Logged]) -> bool:
        return q in self._overflowed

    def close(self, q: asyncio.Queue[Logged]) -> None:
        self._listeners.discard(q)
        self._overflowed.discard(q)


def events_router(
    log: EventLog, still_authorized: Callable[[Request], bool] = is_authorized
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_owner)])

    @router.get("/api/events")
    async def events(
        request: Request,
        topics: Annotated[str, Query(min_length=1)],
        after: Annotated[int, Query(ge=0)] = 0,
        follow: bool = True,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        wanted = {t.strip() for t in topics.split(",") if t.strip()}
        unknown = sorted(wanted - log.topics)
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"not streamable: {', '.join(unknown)}"
            )
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def body() -> AsyncIterator[bytes]:
            queue = log.listen()  # before replay, so nothing falls between the two
            try:
                sent = start
                for e in log.since(start, wanted):
                    sent = e.id
                    yield e.frame()
                while follow:
                    if log.overflowed(queue) and queue.empty():
                        return
                    try:
                        e = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        if await request.is_disconnected() or not still_authorized(request):
                            return
                        yield b": keepalive\n\n"
                        continue
                    if not still_authorized(request):
                        return
                    if e.id > sent and e.topic in wanted:
                        sent = e.id
                        yield e.frame()
            finally:
                log.close(queue)

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
