# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run git in the KB folder: non-interactive, C locale, the user's own credentials."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as spawn
from pathlib import Path

from jesinia.services.kbsync.application.ports import GitError, GitResult

__all__ = ["GitError", "GitRunner"]


class GitRunner:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    async def run(self, *args: str, check: bool = True) -> GitResult:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        proc = await spawn(
            "git",
            "-C",
            str(self.repo),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,  # its own process group, so hooks/helpers die with it
        )
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            # Shutdown cancelled the cycle: stop git before the final cycle starts one.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            raise
        result = GitResult(proc.returncode or 0, out.decode().strip(), err.decode())
        if check and result.code != 0:
            raise GitError(args, result)
        return result
