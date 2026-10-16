# SPDX-License-Identifier: AGPL-3.0-or-later
"""``jesinia update``: keep an install on the latest ``main`` (run by the update agent).

Trust model: this runs whatever is on ``origin/main`` of the remote the install was set
up from (the same trust as ``git pull && ./setup``); a changed ``origin`` is refused.
Only a clean checkout on ``main`` that can fast-forward is updated. After pulling it
rebuilds (``make install build``) and restarts the server; if the build or the restarted
server fails (including a 200 ``/healthz`` that reports a required module down), it goes
back to the previous commit, rebuilds that, and restarts again.

The required modules come from the candidate's own code (``jesinia required-modules`` run
from the updated checkout), not from this process, which imported the old version.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from jesinia.cli.install import LABEL, REPO, System, unhealthy_modules
from jesinia.core.config import Config

HEALTH_TIMEOUT = 60.0


def _healthy(
    cfg: Config, sleep: Callable[[float], None], required: frozenset[str] | None = None
) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{cfg.port}/healthz", timeout=2, trust_env=False)
            # A 200 with a failing required module is a broken update, same as for install.
            if r.status_code == 200 and not unhealthy_modules(r.json(), required):
                return True
        except (httpx.HTTPError, ValueError):
            pass
        sleep(1.0)
    return False


def update(cfg: Config, sys: System, repo: Path = REPO, *, restart: bool = True) -> str:
    """One update attempt; returns what happened (also echoed with a timestamp)."""

    def say(msg: str) -> str:
        sys.echo(f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}")
        return msg

    lock_path = cfg.data_dir / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return say("skipped: another update is running")
        return _update(cfg, sys, repo, say, restart)


def _update(cfg: Config, sys: System, repo: Path, say: Callable[[str], str], restart: bool) -> str:
    git = ["git", "-C", str(repo)]
    code, branch = sys.run([*git, "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if code != 0 or branch != "main":
        return say(f"skipped: {repo} is not on main ({branch or 'no branch'})")
    pinned = sys.environ.get("JESINIA_UPDATE_REMOTE")
    if pinned and sys.run([*git, "remote", "get-url", "origin"], check=False)[1] != pinned:
        return say("skipped: origin is not the remote this install was set up from")
    if sys.run([*git, "status", "--porcelain", "--untracked-files=no"], check=False)[1]:
        return say("skipped: the checkout has local changes")
    fetch = [*git, "fetch", "--quiet", "origin", "main"]
    if sock := _ssh_auth_sock(sys):
        fetch = ["env", f"SSH_AUTH_SOCK={sock}", *fetch]
    if sys.run(fetch, check=False)[0] != 0:
        return say("skipped: could not fetch origin/main")
    old = sys.run([*git, "rev-parse", "HEAD"])[1]
    new = sys.run([*git, "rev-parse", "origin/main"])[1]
    if old == new:
        return say("up to date")
    if sys.run([*git, "merge-base", "--is-ancestor", "HEAD", "origin/main"], check=False)[0] != 0:
        return say("skipped: local main has diverged from origin/main")

    sys.run([*git, "merge", "--ff-only", "--quiet", "origin/main"])
    built = sys.run(["make", "-C", str(repo), "install", "build"], check=False)[0] == 0
    if built and (not restart or _restart(cfg, sys, _candidate_required(sys, repo))):
        return say(f"updated {old[:8]} → {new[:8]}")

    # Roll back to the version that was running.
    stage = "build" if not built else "restart"
    sys.run([*git, "reset", "--hard", "--quiet", old])
    if sys.run(["make", "-C", str(repo), "install", "build"], check=False)[0] != 0:
        # Don't touch a server that may still be running fine on the old build.
        return say(
            f"failed: {new[:8]} did not {stage} cleanly and rebuilding {old[:8]} also failed; "
            "the server was left as it was - run `make install build` and check update.log"
        )
    if not built:
        # The running server never changed: no restart needed.
        return say(f"failed: {new[:8]} did not build cleanly; rolled back to {old[:8]}")
    if restart and not _restart(cfg, sys):
        return say(
            f"failed: {new[:8]} did not restart cleanly, and {old[:8]} is not healthy either"
        )
    return say(f"failed: {new[:8]} did not restart cleanly; rolled back to {old[:8]}")


def _ssh_auth_sock(sys: System) -> str | None:
    """The agent socket for ``git fetch`` over SSH, or None to fetch with the inherited env.

    launchd jobs don't get the login shell's environment, and a path saved at install time
    goes stale after logout or reboot, so ask launchd for the current one. Best effort:
    without it, an HTTPS remote still works and an SSH fetch is skipped as before.
    """
    if sys.environ.get("SSH_AUTH_SOCK"):
        return None  # already inherited: git sees it
    code, sock = sys.run(["launchctl", "getenv", "SSH_AUTH_SOCK"], check=False)
    return sock if code == 0 and sock else None


def _candidate_required(sys: System, repo: Path) -> frozenset[str]:
    """Required modules as the updated checkout defines them (it may rename or drop some).

    If the candidate can't say, an empty set: then every non-optional module its
    ``/healthz`` reports must be ok (see ``unhealthy_modules``).
    """
    uv = sys.which("uv") or "uv"
    argv = [uv, "run", "--quiet", "--project", str(repo / "server"), "jesinia"]
    code, out = sys.run([*argv, "required-modules"], check=False)
    try:
        names = json.loads(out) if code == 0 else None
    except ValueError:
        names = None
    if isinstance(names, list) and all(isinstance(n, str) for n in names):
        return frozenset(names)
    sys.echo("note: the new version did not list its required modules; checking all it reports")
    return frozenset()


def _restart(cfg: Config, sys: System, required: frozenset[str] | None = None) -> bool:
    """Restart and wait for health; ``required`` None means this (running) code's set."""
    sys.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=False)
    return _healthy(cfg, sys.sleep, required)
