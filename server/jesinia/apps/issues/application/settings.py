# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Issues' own settings (PWA Task 14): which agent runner answers suggestion asks.

Unset, suggestions ask the agents service's preferred runner (looked up per ask, so a runner
that becomes available is used); chosen, they ask that one, available or not (an unavailable
one makes the runs wait or fail with its cause, which the author sees)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class SettingsStore(Protocol):
    def runner(self) -> str | None: ...

    def set_runner(self, runner: str | None) -> None: ...


@dataclass(frozen=True)
class IssuesSettings:
    """``runner``: the chosen runner, or ``None`` for the agents service's preferred one."""

    runner: str | None


class IssuesSettingsService:
    """The settings, stored in ``issues.sqlite``.

    Args:
        store: where the choice is kept.
        preferred: the agents service's preferred runner now (``None`` while there is none).
    """

    def __init__(self, store: SettingsStore, preferred: Callable[[], str | None]) -> None:
        self._store = store
        self._preferred = preferred

    def settings(self) -> IssuesSettings:
        return IssuesSettings(self._store.runner())

    def update(self, *, runner: str | None) -> IssuesSettings:
        """Choose ``runner`` (already validated at the boundary), or ``None`` to follow the
        agents service's preference again."""
        self._store.set_runner(runner)
        return self.settings()

    def runner(self) -> str | None:
        """The runner a suggestion ask uses now: the chosen one, else the preferred one."""
        return self._store.runner() or self._preferred()
