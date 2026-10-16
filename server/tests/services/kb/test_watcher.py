# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import logging
from pathlib import Path

import pytest

from jesinia.services.kb.infrastructure.watcher import KbWatcher


async def _wait_for(cond, timeout: float = 4.0) -> bool:  # type: ignore[no-untyped-def]
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_reports_changed_paths_and_activity(tmp_path: Path) -> None:
    got: list[set[Path]] = []

    async def on_paths(paths: set[Path]) -> None:
        got.append(paths)

    w = KbWatcher(tmp_path, on_paths, debounce_ms=50)
    await w.start()
    await w.start()  # idempotent
    await asyncio.sleep(0.2)
    (tmp_path / "a.md").write_text("x")
    assert await _wait_for(lambda: any(p.name == "a.md" for batch in got for p in batch))
    assert w.last_activity is not None
    await w.stop()
    await w.stop()  # idempotent


async def test_handler_errors_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    async def boom(paths: set[Path]) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("handler broke")

    w = KbWatcher(tmp_path, boom, debounce_ms=50)
    with caplog.at_level(logging.ERROR):
        await w.start()
        await asyncio.sleep(0.2)
        (tmp_path / "b.md").write_text("x")
        assert await _wait_for(lambda: calls > 0)
        await w.stop()
    assert "handler broke" in caplog.text
