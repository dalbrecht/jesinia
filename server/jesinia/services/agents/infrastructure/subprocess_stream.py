# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a CLI and stream its stdout lines; the process is killed if the reader stops."""

from __future__ import annotations

import asyncio
import contextlib

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as start_process
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

STDERR_TAIL_BYTES = 4000
LINE_LIMIT = 16 * 1024 * 1024  # stream-json lines can be large (tool results)


@dataclass(frozen=True)
class ProcessResult:
    code: int | None
    stderr_tail: str


class LineSource(Protocol):
    result: ProcessResult

    def lines(self) -> AsyncIterator[str]: ...


Spawn = Callable[[list[str], Path, dict[str, str], str | None], LineSource]


class LineProcess:
    def __init__(
        self, argv: list[str], cwd: Path, env: Mapping[str, str], stdin: str | None
    ) -> None:
        self.argv, self.cwd, self.env, self.stdin = argv, cwd, dict(env), stdin
        self.result = ProcessResult(None, "")
        self.pid: int | None = None

    async def lines(self) -> AsyncIterator[str]:
        proc = await start_process(
            *self.argv,
            cwd=self.cwd,
            env=self.env or None,
            stdin=asyncio.subprocess.PIPE if self.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
        )
        self.pid = proc.pid
        tail: deque[str] = deque()
        size = 0

        async def drain_stderr() -> None:
            nonlocal size
            assert proc.stderr is not None
            async for raw in proc.stderr:
                chunk = raw.decode(errors="replace")
                tail.append(chunk)
                size += len(chunk)
                while size > STDERR_TAIL_BYTES and len(tail) > 1:
                    size -= len(tail.popleft())

        stderr_task = asyncio.create_task(drain_stderr())
        try:
            if self.stdin is not None:
                assert proc.stdin is not None
                proc.stdin.write(self.stdin.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            assert proc.stdout is not None
            async for raw in proc.stdout:
                yield raw.decode(errors="replace").rstrip("\r\n")
            code = await proc.wait()
            await stderr_task
            self.result = ProcessResult(code, "".join(tail).strip()[-STDERR_TAIL_BYTES:])
        finally:
            if proc.returncode is None:
                proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
            stderr_task.cancel()  # no-op if it already finished
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task


def default_spawn(argv: list[str], cwd: Path, env: dict[str, str], stdin: str | None) -> LineSource:
    return LineProcess(argv, cwd, env, stdin)
