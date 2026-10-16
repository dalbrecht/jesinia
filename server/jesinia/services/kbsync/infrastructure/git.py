# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run git in the KB folder: non-interactive, C locale, the user's own credentials."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from jesinia.services.kbsync.application.ports import GitResult


class GitError(RuntimeError):
    def __init__(self, args: tuple[str, ...], result: GitResult) -> None:
        super().__init__(f"git {' '.join(args)} failed ({result.code}): {result.err.strip()}")
        self.args_ = args
        self.result = result


class GitRunner:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    async def run(self, *args: str, check: bool = True) -> GitResult:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        proc = await asyncio.create_subprocess_exec(
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
