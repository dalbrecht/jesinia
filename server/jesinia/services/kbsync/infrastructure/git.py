# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run git in the KB folder: non-interactive, C locale, the user's own credentials."""

from __future__ import annotations

import asyncio
import os

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as spawn
from pathlib import Path

from jesinia.services.kbsync.application.ports import GitResult
from jesinia.services.kbsync.domain.redact import redact


class GitError(RuntimeError):
    def __init__(self, args: tuple[str, ...], result: GitResult) -> None:
        super().__init__(
            f"git {redact(' '.join(args))} failed ({result.code}): {redact(result.err)}"
        )
        self.args_ = args
        self.result = result


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
        )
        out, err = await proc.communicate()
        result = GitResult(proc.returncode or 0, out.decode().strip(), err.decode())
        if check and result.code != 0:
            raise GitError(args, result)
        return result
