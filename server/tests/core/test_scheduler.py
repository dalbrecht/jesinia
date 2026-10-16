# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import logging
from datetime import timedelta

import pytest

from jesinia.core.scheduler import Scheduler


async def test_scheduler_runs_and_stops() -> None:
    runs: list[int] = []

    async def job() -> None:
        runs.append(1)

    s = Scheduler()
    s.every("tick", timedelta(milliseconds=10), job)
    await s.start()
    await asyncio.sleep(0.05)
    await s.stop()
    count = len(runs)
    assert count >= 2
    await asyncio.sleep(0.03)
    assert len(runs) == count


async def test_run_now_and_unknown_job() -> None:
    runs: list[int] = []

    async def job() -> None:
        runs.append(1)

    s = Scheduler()
    s.every("sync", timedelta(hours=4), job)
    await s.run_now("sync")
    assert runs == [1]
    with pytest.raises(KeyError):
        await s.run_now("missing")


async def test_duplicate_name_rejected() -> None:
    async def job() -> None:
        return None

    s = Scheduler()
    s.every("x", timedelta(seconds=1), job)
    with pytest.raises(ValueError):
        s.every("x", timedelta(seconds=1), job)


async def test_failing_job_keeps_running(caplog: pytest.LogCaptureFixture) -> None:
    runs: list[int] = []

    async def job() -> None:
        runs.append(1)
        raise RuntimeError("job failed")

    s = Scheduler()
    s.every("bad", timedelta(milliseconds=10), job)
    with caplog.at_level(logging.ERROR):
        await s.start()
        await asyncio.sleep(0.05)
        await s.stop()
    assert len(runs) >= 2 and "job failed" in caplog.text


async def test_run_now_does_not_overlap_scheduled_run() -> None:
    active = 0
    peak = 0

    async def job() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1

    s = Scheduler()
    s.every("slow", timedelta(milliseconds=5), job)
    await s.start()
    await asyncio.gather(s.run_now("slow"), s.run_now("slow"))
    await s.stop()
    assert peak == 1


async def test_stop_without_start_is_safe() -> None:
    await Scheduler().stop()


async def test_start_twice_and_jobs_added_after_start() -> None:
    async def job() -> None:
        return None

    s = Scheduler()
    s.every("a", timedelta(hours=1), job)
    await s.start()
    await s.start()  # already running: no second task
    s.every("b", timedelta(hours=1), job)  # never started
    await s.stop()


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_nonpositive_interval_rejected(interval: timedelta) -> None:
    async def job() -> None:
        return None

    with pytest.raises(ValueError, match="positive"):
        Scheduler().every("x", interval, job)
