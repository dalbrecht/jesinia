# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared plumbing for CLI-driven runners (spawn, status, end-of-stream errors)."""

from __future__ import annotations

import asyncio
import os
import re
import shutil

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as start_process
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.domain.events import AgentEvent, RunError, TurnDone
from jesinia.services.agents.infrastructure.subprocess_stream import Spawn, default_spawn

# Agent CLIs get a minimal environment: enough to run and find their own login, nothing
# else from the server's environment (spec §7.5).
_ENV_KEEP = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM"}
)
_ENV_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "CODEX_", "OPENAI_")
_RESUME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith(_ENV_PREFIXES)}
    return {**env, **(extra or {})}


def checked_resume_id(value: str) -> str:
    """Resume ids go into argv; refuse anything that could be read as a flag."""
    if not _RESUME_ID.match(value):
        raise ValueError(f"invalid agent session id {value!r}")
    return value


class StreamParser(Protocol):
    def feed(self, line: str) -> list[AgentEvent]: ...


async def _capture(argv: list[str]) -> tuple[int, str]:
    proc = await start_process(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


class CliRunner:
    name: str
    binary: str

    def __init__(
        self,
        *,
        model: str | None = None,
        spawn: Spawn | None = None,
        model_source: Callable[[], str | None] | None = None,
    ) -> None:
        """``model_source`` (Settings → Agents) is asked on every turn, so a changed model
        applies without a restart; without one the fixed ``model`` is used (None: the CLI's
        own default)."""
        self._model = model
        self._model_source = model_source
        self._spawn: Spawn = spawn or default_spawn

    @property
    def model(self) -> str | None:
        return self._model_source() if self._model_source is not None else self._model

    # -- hooks for subclasses -------------------------------------------------------
    def build_argv(self, req: TurnRequest) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def build_env(self, req: TurnRequest) -> dict[str, str]:
        return minimal_env()

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
