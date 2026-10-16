# SPDX-License-Identifier: AGPL-3.0-or-later
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jesinia.core.events import Event, EventBus
from jesinia.services.kbsync.application.ports import GitResult
from jesinia.services.kbsync.application.service import SyncService
from jesinia.services.kbsync.infrastructure.git import GitRunner
from tests.services.kbsync.conftest import git


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


class Spy:
    def __init__(self, inner: GitRunner) -> None:
        self.inner = inner
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args: str, check: bool = True) -> GitResult:
        self.calls.append(args)
        return await self.inner.run(*args, check=check)


class Env:
    def __init__(self, work: Path, *, activity: datetime | None = None) -> None:
        self.work = work
        self.clock = FakeClock()
        self.spy = Spy(GitRunner(work))
        self.activity = activity
        self.events: list[Event] = []
        bus = EventBus()

        async def rec(e: Event) -> None:
            self.events.append(e)

        bus.subscribe("kbsync.status", rec)
        self.sync = SyncService(
            root=work,
            git=self.spy,
            interval=timedelta(hours=4),
            quiet=timedelta(seconds=30),
            bus=bus,
            clock=self.clock,
            activity=lambda: self.activity,
        )


@pytest.fixture
def env(repo_with_remote: Path) -> Env:
    return Env(repo_with_remote)


def fingerprint(repo: Path) -> tuple[str, ...]:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    refs = subprocess.run(
        ["git", "-C", str(repo), "show-ref"], capture_output=True, text=True
    ).stdout
    index = (repo / ".git/index").read_bytes().hex()
    return head, refs, index


async def test_clean_tree_skips_without_network(env: Env) -> None:
    r = await env.sync.run_cycle("timer")
    assert r.outcome == "skipped"
    assert not any(c[0] in {"fetch", "pull", "push", "add", "commit"} for c in env.spy.calls)


async def test_commit_and_push(env: Env, remote: Path) -> None:
    (env.work / "Notes/a.md").write_text("x\n")
    (env.work / "Notes/new.md").write_text("n\n")
    r = await env.sync.run_cycle("manual")
    assert r.outcome == "pushed", r.message
    body = git(remote, "log", "-1", "--format=%B", "main")
    assert body.startswith("kb: 1 added, 1 updated (Notes/…)") and "Jesinia-Auto-Sync: true" in body
    st = env.sync.status()
    assert st.last_result == r and st.consecutive_failures == 0 and not st.state.dirty
    last = env.events[-1].payload["last_result"]
    assert isinstance(last, dict) and last["outcome"] == "pushed"


async def test_waits_for_quiet(env: Env) -> None:
    env.activity = env.clock.now() - timedelta(seconds=5)  # edited 5s ago
    (env.work / "Notes/a.md").write_text("x\n")
    r = await env.sync.run_cycle("timer")
    assert r.outcome == "pushed" and sum(env.clock.slept) == pytest.approx(25)


async def test_shutdown_skips_quiet_wait(env: Env) -> None:
    env.activity = env.clock.now()
    (env.work / "Notes/a.md").write_text("x\n")
    assert (await env.sync.run_cycle("shutdown")).outcome == "pushed" and env.clock.slept == []


def make_conflicting_edits(work: Path, other: Path, rel: str) -> None:
    git(other, "pull", "-q")
    (other / rel).write_text("theirs\n")
    git(other, "commit", "-qam", "theirs")
    git(other, "push", "-q")
    (work / rel).write_text("mine\n")


async def test_rebase_conflict_aborts_and_pauses(env: Env, other_clone: Path) -> None:
    make_conflicting_edits(env.work, other_clone, "Notes/a.md")
    r = await env.sync.run_cycle("manual")
    assert r.outcome == "conflict" and env.sync.status().paused
    assert not (env.work / ".git/rebase-merge").exists()
    assert (env.work / "Notes/a.md").read_text() == "mine\n"
    assert (await env.sync.run_cycle("timer")).outcome == "skipped"  # paused
    await env.sync.resume()
    assert not env.sync.status().paused


async def test_pull_brings_remote_changes(env: Env, other_clone: Path) -> None:
    (other_clone / "Notes/b.md").write_text("from other\n")
    git(other_clone, "add", "-A")
    git(other_clone, "commit", "-qm", "other")
    git(other_clone, "push", "-q")
    (env.work / "Notes/mine.md").write_text("mine\n")
    assert (await env.sync.run_cycle("manual")).outcome == "pushed"
    assert (env.work / "Notes/b.md").read_text() == "from other\n"


def apply_state(work: Path, setup: str) -> None:
    if setup == "detach":
        git(work, "checkout", "-q", "--detach")
    elif setup == "start_rebase":
        (work / ".git/rebase-merge").mkdir()
    else:
        git(work, "branch", "--unset-upstream")


@pytest.mark.parametrize("setup", ["detach", "start_rebase", "unset_upstream"])
async def test_odd_states_block_without_touching_repo(env: Env, setup: str) -> None:
    apply_state(env.work, setup)
    before = fingerprint(env.work)
    (env.work / "Notes/a.md").write_text("x\n")
    r = await env.sync.run_cycle("manual")
    assert r.outcome in {"blocked", "committed"} and env.sync.status().state.problem
    if setup != "unset_upstream":
        assert r.outcome == "blocked" and fingerprint(env.work) == before
    else:
        assert r.outcome == "committed"
    assert not any(c[0] in {"pull", "push"} for c in env.spy.calls)


async def test_commit_only_mode_never_pushes(repo_no_remote: Path) -> None:
    e = Env(repo_no_remote)
    (repo_no_remote / "Notes/a.md").write_text("x\n")
    assert (await e.sync.run_cycle("manual")).outcome == "committed"
    assert not any(c[0] in {"pull", "push"} for c in e.spy.calls)


async def test_backoff_after_push_failure(env: Env, remote: Path) -> None:
    git(env.work, "remote", "set-url", "origin", str(remote.parent / "missing.git"))
    for minutes in (5, 10, 20):
        (env.work / "Notes/a.md").write_text(f"x{minutes}\n")
        r = await env.sync.run_cycle("timer")
        assert r.outcome == "error"
        st = env.sync.status()
        assert st.next_run == env.clock.now() + timedelta(minutes=minutes)
    for _ in range(10):
        await env.sync.run_cycle("timer")
    assert env.sync.status().next_run == env.clock.now() + timedelta(hours=4)  # capped


async def test_never_force_pushes(env: Env, other_clone: Path) -> None:
    (env.work / "Notes/a.md").write_text("x\n")
    await env.sync.run_cycle("manual")
    make_conflicting_edits(env.work, other_clone, "Notes/a.md")
    await env.sync.run_cycle("manual")
    assert all("--force" not in " ".join(c) and "-f" not in c for c in env.spy.calls)


async def test_tick_runs_only_when_due(env: Env) -> None:
    (env.work / "Notes/a.md").write_text("x\n")
    assert await env.sync.tick() is None  # first due time is now + interval
    env.clock.t += timedelta(hours=4)
    r = await env.sync.tick()
    assert r is not None and r.outcome == "pushed"


async def test_off_when_not_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    e = Env(plain)
    assert (await e.sync.run_cycle("manual")).outcome == "skipped"
    assert e.sync.status().state.mode == "off"
    st = await e.sync.init_repo()
    assert st.mode == "commit" and (plain / ".git").is_dir()
    log = subprocess.run(
        ["git", "-C", str(plain), "log", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert "Jesinia-Auto-Sync: true" in log


async def test_init_repo_refuses_existing_repo(env: Env) -> None:
    with pytest.raises(ValueError):
        await env.sync.init_repo()


async def test_no_root(tmp_path: Path) -> None:
    sync = SyncService(
        root=None,
        git=None,
        interval=timedelta(hours=4),
        quiet=timedelta(seconds=30),
        bus=EventBus(),
        clock=FakeClock(),
        activity=lambda: None,
    )
    assert (await sync.run_cycle("manual")).outcome == "skipped"
    with pytest.raises(ValueError):
        await sync.init_repo()


async def test_rebase_started_during_quiet_wait_blocks(env: Env) -> None:
    env.activity = env.clock.now()
    (env.work / "Notes/a.md").write_text("x\n")
    real_sleep = env.clock.sleep

    async def user_starts_rebase(seconds: float) -> None:
        (env.work / ".git/rebase-merge").mkdir(exist_ok=True)
        await real_sleep(seconds)

    env.clock.sleep = user_starts_rebase  # type: ignore[method-assign]
    before = fingerprint(env.work)
    r = await env.sync.run_cycle("timer")
    assert r.outcome == "blocked" and fingerprint(env.work) == before
    assert not any(c[0] == "add" for c in env.spy.calls)


async def test_checked_git_failure_becomes_error_result(env: Env) -> None:
    (env.work / "Notes/a.md").write_text("x\n")
    real = env.spy.run

    async def failing(*args: str, check: bool = True) -> GitResult:
        if args[:2] == ("diff", "--cached"):
            return await real("diff", "--cached", "--no-such-flag", check=True)
        return await real(*args, check=check)

    env.spy.run = failing  # type: ignore[method-assign]
    r = await env.sync.run_cycle("manual")
    assert r.outcome == "error" and r.message.startswith("git failed:")
    st = env.sync.status()
    assert st.consecutive_failures == 1 and st.last_result == r
    assert st.next_run == env.clock.now() + timedelta(minutes=5)
    assert env.events and env.events[-1].payload["last_result"]["outcome"] == "error"  # type: ignore[index]


async def test_unmerged_files_after_autostash_pause_instead_of_push(env: Env) -> None:
    (env.work / "Notes/a.md").write_text("x\n")
    real = env.spy.run

    async def autostash_conflict(*args: str, check: bool = True) -> GitResult:
        if "--diff-filter=U" in args:
            return GitResult(0, "Notes/a.md", "")
        return await real(*args, check=check)

    env.spy.run = autostash_conflict  # type: ignore[method-assign]
    r = await env.sync.run_cycle("manual")
    assert r.outcome == "conflict" and env.sync.status().paused
    assert not any(c[0] == "push" for c in env.spy.calls)


async def test_root_change_during_quiet_wait_never_touches_old_repo(
    env: Env, tmp_path: Path
) -> None:
    env.activity = env.clock.now()
    (env.work / "Notes/a.md").write_text("x\n")
    real_sleep = env.clock.sleep

    async def user_switches_kb(seconds: float) -> None:
        env.sync.configure(tmp_path, GitRunner(tmp_path))
        await real_sleep(seconds)

    env.clock.sleep = user_switches_kb  # type: ignore[method-assign]
    before = fingerprint(env.work)
    r = await env.sync.run_cycle("timer")
    assert r.outcome == "skipped" and "changed" in r.message and fingerprint(env.work) == before


async def test_backoff_never_overflows(env: Env) -> None:
    env.sync._failures = 60
    real = env.spy.run

    async def failing(*args: str, check: bool = True) -> GitResult:
        if args[0] == "add":
            raise OSError("index.lock exists")
        return await real(*args, check=check)

    env.spy.run = failing  # type: ignore[method-assign]
    (env.work / "Notes/a.md").write_text("x\n")
    r = await env.sync.run_cycle("manual")
    assert r.outcome == "error"
    assert env.sync.status().next_run == env.clock.now() + timedelta(hours=4)
