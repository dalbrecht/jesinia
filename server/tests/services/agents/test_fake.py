# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.domain.events import AgentEvent, TextDelta, TurnDone
from jesinia.services.agents.infrastructure.fake import FakeRunner
from jesinia.services.agents.infrastructure.subprocess_stream import LineProcess


async def test_fake_runner_replays_script(req: TurnRequest) -> None:
    script: list[AgentEvent] = [TextDelta("hi"), TurnDone("fake-1", "fake")]
    r = FakeRunner(script, delay_ms=1)
    assert [e async for e in r.run(req)] == script
    assert r.last_req == req and (await r.status()).available and r.name == "fake"


async def test_line_process_real_subprocess(tmp_path: Path) -> None:
    code = (
        "import sys; print('a'); print('b', flush=True); print('err', file=sys.stderr); sys.exit(3)"
    )
    p = LineProcess([sys.executable, "-c", code], tmp_path, {}, None)
    assert [line async for line in p.lines()] == ["a", "b"]
    assert p.result.code == 3 and "err" in p.result.stderr_tail


async def test_line_process_feeds_stdin(tmp_path: Path) -> None:
    code = "import sys; print(sys.stdin.read().upper())"
    p = LineProcess([sys.executable, "-c", code], tmp_path, {}, "hello")
    assert [line async for line in p.lines()] == ["HELLO"] and p.result.code == 0


async def test_line_process_killed_on_cancel(tmp_path: Path) -> None:
    code = "import time\nprint('start', flush=True)\ntime.sleep(30)"
    p = LineProcess([sys.executable, "-c", code], tmp_path, {}, None)

    async def consume() -> None:
        async for _ in p.lines():
            await asyncio.sleep(10)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert p.pid is not None
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(p.pid, 0)


async def test_stderr_tail_is_bounded(tmp_path: Path) -> None:
    from jesinia.services.agents.infrastructure.subprocess_stream import STDERR_TAIL_BYTES

    code = "import sys\nfor i in range(400): print('x' * 50, i, file=sys.stderr)\nprint('done')"
    p = LineProcess([sys.executable, "-c", code], tmp_path, {}, None)
    assert [line async for line in p.lines()] == ["done"]
    assert len(p.result.stderr_tail) <= STDERR_TAIL_BYTES and p.result.stderr_tail.endswith("399")


async def test_cancel_while_stderr_still_streaming(tmp_path: Path) -> None:
    code = (
        "import sys, time\nprint('go', flush=True)\n"
        "while True:\n    print('noise', file=sys.stderr, flush=True)\n    time.sleep(0.01)"
    )
    p = LineProcess([sys.executable, "-c", code], tmp_path, {}, None)
    gen = p.lines()
    assert await anext(gen) == "go"
    assert isinstance(gen, AsyncGenerator)
    await gen.aclose()  # consumer stops early: process killed, stderr reader cancelled


def test_default_spawn_builds_line_process(tmp_path: Path) -> None:
    from jesinia.services.agents.infrastructure.subprocess_stream import default_spawn

    assert isinstance(default_spawn(["true"], tmp_path, {}, None), LineProcess)


async def test_capture_runs_a_real_binary() -> None:
    from jesinia.services.agents.infrastructure.cli import _capture

    code, out = await _capture([sys.executable, "--version"])
    assert code == 0 and out.startswith("Python")
