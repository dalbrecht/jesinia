# SPDX-License-Identifier: AGPL-3.0-or-later
"""What kbsync needs to run git; implemented by ``infrastructure.git.GitRunner``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str
    err: str


class Git(Protocol):
    async def run(self, *args: str, check: bool = True) -> GitResult: ...
