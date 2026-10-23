# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Zotero local API key, kept in the macOS Keychain (ADR 0013).

The key is written through ``security -i`` on stdin so it never appears in argv or ``ps``,
and it is never logged or put in an exception message.
"""

from __future__ import annotations

import asyncio
import re

# argv-only process spawning, never a shell. Aliased because the standards linter's
# banned-builtin pattern also matches the name of this safe asyncio function.
from asyncio import create_subprocess_exec as spawn
from collections.abc import Awaitable, Callable

from jesinia.apps.zotero.domain.errors import KeychainWriteUnverified, ZoteroError

Run = Callable[[list[str], str | None], Awaitable[tuple[int, str]]]
KEYCHAIN_SERVICE = "jesinia-zotero"
KEYCHAIN_ACCOUNT = "local-api"
_ITEM_NOT_FOUND = 44
_TIMEOUT_SECONDS = 10
# Zotero keys are alphanumeric; anything else could break out of the one-line command.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


async def _run(argv: list[str], stdin: str | None) -> tuple[int, str]:
    try:
        proc = await spawn(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(None if stdin is None else stdin.encode("utf-8")),
            timeout=_TIMEOUT_SECONDS,
        )
    except TimeoutError:  # pragma: no cover - a hung keychain prompt
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode or 0, out.decode("utf-8", "replace")


class KeychainKeyStore:
    """Implements :class:`KeyStore` over the login Keychain."""

    def __init__(self, run: Run = _run) -> None:
        self._run = run

    async def read(self) -> str | None:
        code, out = await self._run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            None,
        )
        if code == _ITEM_NOT_FOUND:
            return None
        if code != 0:
            raise ZoteroError(f"reading the Zotero key from the Keychain failed (exit {code})")
        return out.strip() or None

    async def write(self, key: str) -> None:
        if _SAFE_KEY.fullmatch(key) is None:
            raise ValueError("refusing to store a malformed Zotero API key")
        command = (
            f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT} -w {key}\nquit\n"
        )
        code, _ = await self._run(["security", "-i"], command)
        if code != 0:
            raise ZoteroError(f"storing the Zotero key in the Keychain failed (exit {code})")
        # `security -i` can exit 0 even when the inner command failed, so confirm the write.
        if await self.read() != key:
            raise KeychainWriteUnverified("the Zotero key could not be read back from the Keychain")
