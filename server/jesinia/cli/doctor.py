# SPDX-License-Identifier: AGPL-3.0-or-later
"""``jesinia doctor``: check the install and say how to fix what is wrong."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from jesinia.core.config import Config, ConfigError, load_config

Level = Literal["ok", "warn", "fail"]
Run = Callable[[list[str], float], tuple[int, str]]
LABEL = "com.jesinia.server"


@dataclass(frozen=True)
class Check:
    level: Level
    name: str
    detail: str
    remedy: str | None = None

    def line(self) -> str:
        text = f"{self.level} {self.name}: {self.detail}"
        return f"{text} → {self.remedy}" if self.remedy else text


def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or p.stderr).strip()


def _config(path: Path) -> tuple[list[Check], Config | None]:
    if not path.exists():
        return [Check("fail", "config", f"{path} not found", "run ./setup")], None
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        return [Check("fail", "config", str(exc), f"fix or remove {path}")], None
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        return [
            Check("fail", "config", f"mode {mode:o} (holds the token)", f"chmod 600 {path}")
        ], cfg
    return [Check("ok", "config", str(path))], cfg


def _kb(cfg: Config, run: Run) -> list[Check]:
    remedy = "jesinia config set kb.path /path/to/kb"
    kb = cfg.kb_path
    if kb is None:
        return [Check("fail", "kb.path", "not set", remedy)]
    if not kb.is_dir():
        return [Check("fail", "kb.path", f"{kb} does not exist", remedy)]
    if not os.access(kb, os.W_OK):
        return [Check("fail", "kb.path", f"{kb} is not writable", f"chmod u+w {kb}")]
    checks = [Check("ok", "kb.path", str(kb))]
    code, out = run(["git", "-C", str(kb), "rev-parse", "--is-inside-work-tree"], 10)
    if code != 0 or out != "true":
        checks.append(
            Check(
                "warn", "kb.git", "not a git repository; sync is off", "Settings → Initialize git"
            )
        )
        return checks
    code, remote = run(["git", "-C", str(kb), "remote"], 10)
    if code != 0 or not remote:
        checks.append(Check("ok", "kb.git", "local commits only (no remote)"))
        return checks
    code, out = run(["git", "-C", str(kb), "ls-remote", "--heads", remote.split()[0]], 10)
    if code != 0:
        checks.append(
            Check("warn", "kb.git", f"remote unreachable: {out[:200]}", "check network / git auth")
        )
    else:
        checks.append(Check("ok", "kb.git", f"push to {remote.split()[0]}"))
    return checks


TOOLS: tuple[tuple[str, Level, str], ...] = (
    ("uv", "fail", "brew install uv"),
    ("node", "fail", "brew install node"),
    ("pnpm", "warn", "corepack enable"),
    ("gh", "warn", "brew install gh"),
    ("claude", "warn", "install Claude Code to chat with agents"),
    ("codex", "warn", "install Codex to chat with agents"),
)


def _tools(run: Run, which: Callable[[str], str | None]) -> list[Check]:
    checks: list[Check] = []
    for name, missing, remedy in TOOLS:
        path = which(name)
        if path is None:
            checks.append(Check(missing, name, "not found on PATH", remedy))
            continue
        code, out = run([path, "--version"], 10)
        version = out.splitlines()[0] if code == 0 and out else "version unknown"
        checks.append(Check("ok", name, version))
        if name == "gh":
            code, out = run([path, "auth", "status"], 10)
            if code != 0:
                checks.append(Check("warn", "gh.auth", "not logged in", "gh auth login"))
            else:
                checks.append(Check("ok", "gh.auth", "logged in"))
    return checks


def _launchd(run: Run) -> Check:
    code, _ = run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], 10)
    if code != 0:
        return Check("warn", "launchd", f"{LABEL} is not loaded", "./setup")
    return Check("ok", "launchd", f"{LABEL} loaded")


def _server(cfg: Config) -> Check:
    try:
        r = httpx.get(f"http://127.0.0.1:{cfg.port}/healthz", timeout=5, trust_env=False)
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return Check(
            "fail", "server", f"not answering on port {cfg.port}", "jesinia serve or ./setup"
        )
    bad = [f"{n}: {m['detail']}" for n, m in body.get("modules", {}).items() if not m["ok"]]
    if bad:
        return Check("warn", "server", "; ".join(bad))
    return Check("ok", "server", f"healthy on port {cfg.port}")


def doctor(
    path: Path, *, run: Run | None = None, which: Callable[[str], str | None] | None = None
) -> list[Check]:
    run = run or run_command
    which = which or shutil.which
    checks, cfg = _config(path)
    if cfg is not None:
        checks += _kb(cfg, run)
    checks += _tools(run, which)
    checks.append(_launchd(run))
    if cfg is not None:
        checks.append(_server(cfg))
    return checks
