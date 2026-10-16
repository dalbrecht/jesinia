# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared plumbing for CLI-driven runners (spawn, status, end-of-stream errors)."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.domain.events import AgentEvent, RunError, TurnDone
from jesinia.services.agents.infrastructure.subprocess_stream import Spawn, default_spawn


class StreamParser(Protocol):
    def feed(self, line: str) -> list[AgentEvent]: ...


async def _capture(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


class CliRunner:
    name: str
    binary: str

    def __init__(self, *, model: str | None = None, spawn: Spawn | None = None) -> None:
        self.model = model
        self._spawn: Spawn = spawn or default_spawn

    # -- hooks for subclasses -------------------------------------------------------
    def build_argv(self, req: TurnRequest) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def build_env(self, req: TurnRequest) -> dict[str, str]:
        return dict(os.environ)

    def stdin(self, req: TurnRequest) -> str:
        return req.prompt

    def parser(self) -> StreamParser:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- shared ---------------------------------------------------------------------
    async def status(self) -> RunnerStatus:
        path = shutil.which(self.binary)
        if path is None:
            return RunnerStatus(False, None, f"{self.binary} is not installed or not on PATH")
        code, out = await _capture([path, "--version"])
        version = out.strip().splitlines()[0] if out.strip() else None
        if code != 0 or version is None:
            return RunnerStatus(False, None, f"{self.binary} --version failed")
        return RunnerStatus(True, version, f"{self.binary} {version}")

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        source = self._spawn(
            self.build_argv(req), Path(req.workdir), self.build_env(req), self.stdin(req)
        )
        parser = self.parser()
        finished = False
        async for line in source.lines():
            for event in parser.feed(line):
                finished = finished or isinstance(event, TurnDone | RunError)
                yield event
        if not finished:
            tail = source.result.stderr_tail
            message = f"{self.name} exited with code {source.result.code}"
            yield RunError(f"{message}: {tail}" if tail else message)
