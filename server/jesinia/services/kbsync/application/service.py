# SPDX-License-Identifier: AGPL-3.0-or-later
"""The KB git sync cycle (spec §4.4).

1. Clean tree and not ahead → skip (no commit, no network).
2. Wait for the KB to be quiet (no watcher activity for ``quiet``), except at shutdown.
3. ``git add -A``; commit with a generated summary and the ``Jesinia-Auto-Sync`` trailer.
4. Push mode: ``git pull --rebase --autostash``. A conflict aborts the rebase (working
   tree restored) and pauses sync until the user resolves it and resumes.
5. ``git push`` — never forced.

Detached HEAD or an in-progress rebase/merge blocks the cycle without touching the repo.
Failures back off exponentially (5 min, 10 min, … capped at the interval).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from jesinia.core.events import Event, EventBus
from jesinia.services.kbsync.application.detect import detect
from jesinia.services.kbsync.application.ports import Git, GitError
from jesinia.services.kbsync.domain.message import commit_message, parse_name_status
from jesinia.services.kbsync.domain.redact import redact
from jesinia.services.kbsync.domain.state import GitState

Outcome = Literal["skipped", "committed", "pushed", "conflict", "blocked", "error"]
Trigger = Literal["timer", "manual", "shutdown"]
BACKOFF_BASE = timedelta(minutes=5)
MAX_BACKOFF_EXPONENT = 16  # 5 min × 2^16 is far beyond any interval


@dataclass(frozen=True)
class SyncResult:
    outcome: Outcome
    message: str


@dataclass(frozen=True)
class SyncStatus:
    state: GitState
    paused: bool
    last_run: datetime | None
    last_result: SyncResult | None
    next_run: datetime | None
    consecutive_failures: int

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["state"]["problem_text"] = self.state.problem_text
        for key in ("last_run", "next_run"):
            value = d[key]
            d[key] = value.isoformat() if isinstance(value, datetime) else None
        return d


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


OFF = GitState("off", None, None, 0, 0, False, None)


class SyncService:
    def __init__(
        self,
        *,
        root: Path | None,
        git: Git | None,
        interval: timedelta,
        quiet: timedelta,
        bus: EventBus,
        clock: Clock | None = None,
        activity: Callable[[], datetime | None] = lambda: None,
    ) -> None:
        self.root = root
        self.git = git
        self.interval = interval
        self.quiet = quiet
        self.bus = bus
        self.clock: Clock = clock or SystemClock()
        self.activity = activity
        self._state = OFF
        self._paused = False
        self._last_run: datetime | None = None
        self._last_result: SyncResult | None = None
        self._failures = 0
        self._next_run: datetime | None = self.clock.now() + interval
        self._lock = asyncio.Lock()

    # -- state ---------------------------------------------------------------------
    def configure(self, root: Path | None, git: Git | None) -> None:
        """Point at a new KB root (``kb.root_changed``)."""
        self.root, self.git = root, git
        self._paused = False
        self._failures = 0

    async def refresh(self) -> GitState:
        self._state = await detect(self.git, self.root) if self.root else OFF
        return self._state

    def status(self) -> SyncStatus:
        return SyncStatus(
            self._state,
            self._paused,
            self._last_run,
            self._last_result,
            self._next_run,
            self._failures,
        )

    async def resume(self) -> None:
        self._paused = False
        await self.refresh()
        await self._publish()

    async def _publish(self) -> None:
        await self.bus.publish(Event("kbsync.status", self.status().to_dict()))

    # -- cycle ---------------------------------------------------------------------
    async def tick(self) -> SyncResult | None:
        """Scheduler hook: run a timer cycle when one is due (honours backoff)."""
        if self._next_run is not None and self.clock.now() < self._next_run:
            return None
        return await self.run_cycle("timer")

    async def run_cycle(self, trigger: Trigger) -> SyncResult:
        async with self._lock:
            try:
                result = await self._cycle(trigger)
            except (GitError, OSError) as exc:
                # A checked git command failed (index lock, permissions, git missing):
                # record it like any other failure so status, backoff and events update.
                result = SyncResult("error", f"git failed: {redact(str(exc))}")
            now = self.clock.now()
            self._last_run, self._last_result = now, result
            if result.outcome == "error":
                self._failures += 1
                # Cap the exponent first: a huge timedelta would overflow before min().
                exponent = min(self._failures - 1, MAX_BACKOFF_EXPONENT)
                delay = min(self.interval, BACKOFF_BASE * 2**exponent)
            else:
                self._failures = 0
                delay = self.interval
            self._next_run = now + delay
            if self.git is not None and self.root is not None:
                await self.refresh()
            await self._publish()
            return result

    async def _wait_for_quiet(self) -> None:
        while True:
            last = self.activity()
            if last is None:
                return
            remaining = (last + self.quiet - self.clock.now()).total_seconds()
            if remaining <= 0:
                return
            await self.clock.sleep(remaining)

    async def _cycle(self, trigger: Trigger) -> SyncResult:
        if self._paused:
            return SyncResult("skipped", "sync is paused: resolve the conflict, then resume")
        state = await self.refresh()
        if state.mode == "off" or self.git is None:
            return SyncResult("skipped", "sync is off (the KB folder is not a git repository)")
        if state.blocked:
            return SyncResult("blocked", state.problem_text or "blocked")
        if not state.dirty and state.ahead == 0:
            return SyncResult("skipped", "nothing to sync")
        git, root = self.git, self.root
        if trigger != "shutdown":
            await self._wait_for_quiet()
            if self.git is not git or self.root != root:
                # The KB folder changed while we waited: never touch the old one.
                return SyncResult(
                    "skipped", "the KB folder changed; the next cycle syncs the new one"
                )
            # The user may have started a rebase or merge (or detached HEAD) while we
            # waited: never touch the index in those states.
            state = await self.refresh()
            if state.blocked:
                return SyncResult("blocked", state.problem_text or "blocked")

        await git.run("add", "-A")
        staged = parse_name_status((await git.run("diff", "--cached", "--name-status", "-M")).out)
        committed = False
        if staged:
            r = await git.run("commit", "-q", "-m", commit_message(staged), check=False)
            if r.code != 0:
                return SyncResult("error", f"commit failed: {redact(r.err)}")
            committed = True

        if state.mode != "push":
            return SyncResult("committed", state.problem_text or "committed locally")

        pull = await git.run("pull", "--rebase", "--autostash", "-q", check=False)
        if pull.code != 0:
            if await self._rebasing():
                await git.run("rebase", "--abort", check=False)
                self._paused = True
                return SyncResult(
                    "conflict",
                    "Sync paused: the KB has a merge conflict. Resolve it, then choose Resume.",
                )
            return SyncResult("error", f"pull failed: {redact(pull.err)}")
        # --autostash can leave conflicted files after a "successful" pull (the stash
        # pop conflicted). Never push, or later commit, conflict markers: pause instead.
        unmerged = await git.run("diff", "--name-only", "--diff-filter=U", check=False)
        if unmerged.out.strip():
            self._paused = True
            return SyncResult(
                "conflict",
                "Sync paused: the KB has a merge conflict. Resolve it, then choose Resume.",
            )
        push = await git.run("push", "-q", check=False)
        if push.code != 0:
            return SyncResult("error", f"push failed: {redact(push.err)}")
        return SyncResult("pushed", "committed and pushed" if committed else "pushed")

    async def _rebasing(self) -> bool:
        assert self.git is not None
        for name in ("rebase-merge", "rebase-apply"):
            r = await self.git.run("rev-parse", "--path-format=absolute", "--git-path", name)
            if Path(r.out).exists():
                return True
        return False

    async def init_repo(self) -> GitState:
        """``Initialize git`` (Settings): only when the KB is not already a repository."""
        if self.root is None or self.git is None:
            raise ValueError("no KB folder is configured")
        if (await self.refresh()).mode != "off":
            raise ValueError("the KB folder is already a git repository")
        git = self.git
        await git.run("init", "-q", "-b", "main")
        await git.run("add", "-A")
        staged = parse_name_status((await git.run("diff", "--cached", "--name-status")).out)
        await git.run("commit", "-q", "--allow-empty", "-m", commit_message(staged))
        state = await self.refresh()
        await self._publish()
        return replace(state)
