# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find a GitHub token without storing one: ``gh auth token``, then the Keychain."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable

Run = Callable[[list[str]], Awaitable[tuple[int, str]]]
KEYCHAIN_SERVICE = "jesinia-github"


async def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:  # pragma: no cover - a hung credential helper
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def github_token(
    run: Run = _run, which: Callable[[str], str | None] = shutil.which
) -> str | None:
    """The current token, or ``None``. Looked up on each call; never persisted."""
    sources = (
        ["gh", "auth", "token"],
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
    )
    for argv in sources:
        binary = which(argv[0])
        if binary is None:
            continue
        code, out = await run([binary, *argv[1:]])
        if code == 0 and out.strip():
            return out.strip()
    return None
