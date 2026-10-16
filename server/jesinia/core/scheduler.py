# SPDX-License-Identifier: AGPL-3.0-or-later
"""Periodic in-process jobs (KB sync, reindex, later RSS polling); spec §6."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]


@dataclass
class _Entry:
    interval: timedelta
    job: Job
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None


class Scheduler:
    """Runs each named job every ``interval`` (first run after one interval).

    A job never overlaps itself: ``run_now`` and the periodic run share a lock. Job
    exceptions are logged and the schedule continues.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _Entry] = {}

    def every(self, name: str, interval: timedelta, job: Job) -> None:
        if name in self._jobs:
            raise ValueError(f"job {name!r} already scheduled")
        self._jobs[name] = _Entry(interval, job)

    async def _run(self, name: str, entry: _Entry) -> None:
        async with entry.lock:
            try:
                await entry.job()
            except Exception:
                log.exception("scheduled job %s failed", name)

    async def _loop(self, name: str, entry: _Entry) -> None:
        while True:
            await asyncio.sleep(entry.interval.total_seconds())
            await self._run(name, entry)

    async def start(self) -> None:
        for name, entry in self._jobs.items():
            if entry.task is None:
                entry.task = asyncio.create_task(self._loop(name, entry), name=f"job:{name}")

    async def stop(self) -> None:
        for entry in self._jobs.values():
            if entry.task is not None:
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.task
                entry.task = None

    async def run_now(self, name: str) -> None:
        await self._run(name, self._jobs[name])
