# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watch the KB folder for external edits (Pigeonpile Wiki, editors, ``git pull``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from watchfiles import awatch

log = logging.getLogger(__name__)

OnPaths = Callable[[set[Path]], Awaitable[None]]


class KbWatcher:
    def __init__(self, root: Path, on_paths: OnPaths, debounce_ms: int = 500) -> None:
        self.root = root
        self.on_paths = on_paths
        self.debounce_ms = debounce_ms
        self.last_activity: datetime | None = None  # read by kbsync's quiet period
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async for changes in awatch(
            self.root, debounce=self.debounce_ms, stop_event=self._stop, recursive=True
        ):
            self.last_activity = datetime.now(UTC)
            paths = {Path(p) for _, p in changes}
            try:
                await self.on_paths(paths)
            except Exception:
                log.exception("KB watcher handler failed")

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="kb-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
