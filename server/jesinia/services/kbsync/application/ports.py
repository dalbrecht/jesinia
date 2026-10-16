# SPDX-License-Identifier: AGPL-3.0-or-later
"""What kbsync needs to run git; implemented by ``infrastructure.git.GitRunner``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jesinia.services.kbsync.domain.redact import redact


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str
    err: str


class GitError(RuntimeError):
    """A checked git command failed; the message is already redacted."""

    def __init__(self, args: tuple[str, ...], result: GitResult) -> None:
        super().__init__(
            f"git {redact(' '.join(args))} failed ({result.code}): {redact(result.err)}"
        )
        self.args_ = args
        self.result = result


class Git(Protocol):
    async def run(self, *args: str, check: bool = True) -> GitResult: ...
