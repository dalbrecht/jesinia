# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the GitHub service needs from the outside world."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

TokenSource = Callable[[], Awaitable[str | None]]


@dataclass(frozen=True)
class CachedResponse:
    etag: str
    status: int
    data: object


class GitHubCache(Protocol):
    def get(self, key: str) -> CachedResponse | None: ...

    def put(self, key: str, etag: str, status: int, data: object) -> None: ...

    def prune(self, older_than: timedelta) -> int: ...
