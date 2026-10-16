# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio

from jesinia.core.app import create_app
from jesinia.core.config import Config
from jesinia.core.events import Event, EventBus
from jesinia.core.stream import EventLog
from tests.conftest import bearer, lifespan_client


async def test_event_log_buffers_and_fans_out() -> None:
    bus = EventBus()
    log = EventLog(bus)
    q = log.listen()
    await bus.publish(Event("kbsync.status", {"paused": True}))
    await bus.publish(Event("not.streamed", {}))
    await bus.publish(Event("kb.page_changed", {"id": "x"}))
    assert [e.id for e in log.since(0, {"kbsync.status", "kb.page_changed"})] == [1, 2]
    assert [e.topic for e in log.since(1, {"kb.page_changed"})] == ["kb.page_changed"]
    assert (await asyncio.wait_for(q.get(), 1)).topic == "kbsync.status"
    log.close(q)
    log.close(q)
    assert (
        b'id: 1\nevent: kbsync.status\ndata: {"paused": true}\n\n'
        == log.since(0, {"kbsync.status"})[0].frame()
    )


async def test_events_route_replays_after_id(cfg: Config) -> None:
    app = create_app(cfg, [], static_root=None)
    async with lifespan_client(app) as c:
        bus = app.state.ctx.bus
        for i in range(3):
            await bus.publish(Event("kb.page_changed", {"n": i}))
        await bus.publish(Event("kbsync.status", {"paused": False}))
        assert (await c.get("/api/events?topics=kb.page_changed")).status_code == 401
        c.headers.update(bearer(cfg.token))
        r = await c.get(
            "/api/events", params={"topics": "kb.page_changed", "after": 1, "follow": "false"}
        )
        assert r.headers["content-type"].startswith("text/event-stream")
        assert (
            r.text.count("event: kb.page_changed") == 2
            and "id: 2\n" in r.text
            and "id: 1\n" not in r.text
        )
        r = await c.get(
            "/api/events",
            params={"topics": "kb.page_changed,kbsync.status", "follow": "false"},
            headers={"Last-Event-ID": "3"},
        )
        assert r.text.startswith("id: 4\nevent: kbsync.status")
        bad = await c.get("/api/events", params={"topics": "agents.secret"})
        assert bad.status_code == 400 and "not streamable" in bad.text


async def test_events_follow_delivers_live_and_keeps_alive(cfg: Config, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from jesinia.core import stream

    monkeypatch.setattr(stream, "KEEPALIVE_SECONDS", 0.01)
    bus = EventBus()
    log = EventLog(bus)
    router = stream.events_router(log, still_authorized=lambda r: True)
    endpoint = next(r for r in router.routes if getattr(r, "path", "") == "/api/events").endpoint  # type: ignore[attr-defined]

    class Req:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 2

    resp = await endpoint(Req(), topics="kb.page_changed", after=0, follow=True, last_event_id=None)
    it = resp.body_iterator
    first = await it.__anext__()
    assert first == b": keepalive\n\n"
    await bus.publish(Event("kbsync.status", {}))  # not requested: skipped
    await bus.publish(Event("kb.page_changed", {"id": "a"}))
    assert (await it.__anext__()).startswith(b"id: 2\nevent: kb.page_changed")
    chunks = [c async for c in it]  # keepalive until the client goes away
    assert all(c == b": keepalive\n\n" for c in chunks)


def _endpoint(log: EventLog, still_authorized):  # type: ignore[no-untyped-def]
    from jesinia.core import stream

    router = stream.events_router(log, still_authorized=still_authorized)
    return next(r for r in router.routes if getattr(r, "path", "") == "/api/events").endpoint  # type: ignore[attr-defined]


class _Connected:
    async def is_disconnected(self) -> bool:
        return False


async def test_a_stalled_listener_is_cut_off_instead_of_queueing_forever(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from jesinia.core import stream

    monkeypatch.setattr(stream, "BUFFER", 2)
    bus = EventBus()
    log = EventLog(bus)
    resp = await _endpoint(log, lambda r: True)(
        _Connected(), topics="kb.page_changed", after=0, follow=True, last_event_id=None
    )
    it = resp.body_iterator
    pending = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0)  # the stream is now listening
    for i in range(5):
        await bus.publish(Event("kb.page_changed", {"n": i}))
    first = await asyncio.wait_for(pending, 1)
    rest = [c async for c in it]  # drains what it queued, then ends: the client reconnects
    assert first.startswith(b"id: 1\n") and len(rest) == 1 and rest[0].startswith(b"id: 2\n")
    q = log.listen()
    assert not log.overflowed(q)


async def test_a_stream_ends_when_its_credentials_are_revoked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from jesinia.core import stream

    monkeypatch.setattr(stream, "KEEPALIVE_SECONDS", 0.01)
    bus = EventBus()
    log = EventLog(bus)
    valid = {"yes": True}
    resp = await _endpoint(log, lambda r: valid["yes"])(
        _Connected(), topics="kb.page_changed", after=0, follow=True, last_event_id=None
    )
    it = resp.body_iterator
    assert await it.__anext__() == b": keepalive\n\n"
    valid["yes"] = False
    await bus.publish(Event("kb.page_changed", {"id": "secret"}))
    assert [c async for c in it] == []  # the event after rotation is never sent
    resp = await _endpoint(log, lambda r: valid["yes"])(
        _Connected(), topics="kb.page_changed", after=99, follow=True, last_event_id=None
    )
    assert [c async for c in resp.body_iterator] == []  # idle stream: ends at keepalive


def test_is_authorized_follows_session_invalidation(cfg: Config) -> None:
    from types import SimpleNamespace

    from jesinia.core import auth

    auth._cookie_sessions.add("sid")
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(cfg=cfg))),
        cookies={auth.COOKIE: "sid"},
        headers={},
    )
    assert auth.is_authorized(req)  # type: ignore[arg-type]
    auth.invalidate_sessions()
    assert not auth.is_authorized(req)  # type: ignore[arg-type]
