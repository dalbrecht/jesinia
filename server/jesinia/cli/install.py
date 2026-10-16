# SPDX-License-Identifier: AGPL-3.0-or-later
"""``jesinia install`` / ``uninstall``: the LaunchAgent and agent registrations.

Everything goes through ``System`` so ``--dry-run`` can print each action instead of
doing it. The KB folder is never deleted or modified here.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from jesinia.cli.server import ServerDown, call
from jesinia.core.config import Config, load_config, save_config
from jesinia.core.paths import ENV_HOME, config_path, default_data_dir, default_log_dir

LABEL = "com.jesinia.server"
MCP_NAME = "jesinia"
REPO = Path(__file__).resolve().parents[3]
HEALTH_TIMEOUT = 20.0


def render_plist(
    uv: Path,
    repo: Path,
    log_dir: Path,
    path_env: str,
    ssh_auth_sock: str | None,
    extra_env: Mapping[str, str] | None = None,
) -> str:
    """The LaunchAgent: run ``jesinia serve`` at login; restart only after a crash."""
    env = {"PATH": path_env, **(extra_env or {})}
    if ssh_auth_sock:
        env["SSH_AUTH_SOCK"] = ssh_auth_sock
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(uv), "run", "--project", str(repo / "server"), "jesinia", "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": env,
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
    }
    return plistlib.dumps(plist).decode()


def _redact(argv: list[str]) -> str:
    return " ".join(
        "Authorization: Bearer ***" if a.startswith("Authorization:") else a for a in argv
    )


@dataclass
class System:
    """Side effects, injectable for tests; ``dry_run`` prints instead of acting."""

    dry_run: bool = False
    yes: bool = False
    echo: Callable[[str], None] = print
    confirm: Callable[[str], bool] = lambda q: input(f"{q} [y/N] ").strip().lower() == "y"
    which: Callable[[str], str | None] = shutil.which
    sleep: Callable[[float], None] = time.sleep
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def run(self, argv: list[str], *, check: bool = True) -> tuple[int, str]:
        if self.dry_run:
            self.echo(f"would run: {_redact(argv)}")
            return 0, ""
        p = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if check and p.returncode != 0:
            raise RuntimeError(f"{_redact(argv)} failed: {(p.stderr or p.stdout).strip()}")
        return p.returncode, (p.stdout or "").strip()

    def write(self, path: Path, text: str, mode: int = 0o644) -> None:
        if self.dry_run:
            self.echo(f"would write: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)

    def mkdir(self, path: Path) -> None:
        if self.dry_run:
            if not path.is_dir():
                self.echo(f"would create: {path}/")
            return
        path.mkdir(parents=True, exist_ok=True)

    def remove(self, path: Path) -> None:
        if self.dry_run:
            if path.exists():
                self.echo(f"would remove: {path}")
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def ask(self, question: str) -> bool:
        return self.yes or self.dry_run or self.confirm(question)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


# -- agent registrations ----------------------------------------------------------------


def mcp_url(cfg: Config) -> str:
    return f"http://127.0.0.1:{cfg.port}/mcp"


def _codex_config(sys: System) -> Path:
    home = sys.environ.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex" / "config.toml"


def register_claude(sys: System, cfg: Config, claude: str) -> None:
    sys.run([claude, "mcp", "remove", "--scope", "user", MCP_NAME], check=False)
    sys.run(
        [claude, "mcp", "add", "--scope", "user", "--transport", "http", MCP_NAME,
         mcp_url(cfg), "--header", f"Authorization: Bearer {cfg.token}"]
    )  # fmt: skip


def register_codex(sys: System, cfg: Config, codex: str) -> None:
    sys.run([codex, "mcp", "remove", MCP_NAME], check=False)
    sys.run([codex, "mcp", "add", MCP_NAME, "--url", mcp_url(cfg)])
    # `codex mcp add` has no header flag; Codex reads static headers from its config.
    path = _codex_config(sys)
    if path.is_dir():
        path = path / "config.toml"
    if sys.dry_run:
        sys.echo(f"would add the Authorization header for {MCP_NAME} to {path}")
        return
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f'\n[mcp_servers.{MCP_NAME}.http_headers]\nAuthorization = "Bearer {cfg.token}"\n'
    _write_private(path, text.rstrip("\n") + "\n" + block)


def _write_private(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a file that is mode 0600 from creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def unregister(sys: System) -> None:
    if claude := sys.which("claude"):
        sys.run([claude, "mcp", "remove", "--scope", "user", MCP_NAME], check=False)
    if codex := sys.which("codex"):
        sys.run([codex, "mcp", "remove", MCP_NAME], check=False)


def refresh_registrations(cfg: Config, sys: System | None = None) -> None:
    """After a token rotation: re-register agents that already have Jesinia."""
    sys = sys or System()
    if (claude := sys.which("claude")) and sys.run([claude, "mcp", "get", MCP_NAME], check=False)[
        0
    ] == 0:
        register_claude(sys, cfg, claude)
        sys.echo("Claude Code: updated")
    if (codex := sys.which("codex")) and sys.run([codex, "mcp", "get", MCP_NAME], check=False)[
        0
    ] == 0:
        register_codex(sys, cfg, codex)
        sys.echo("Codex: updated")


# -- install / uninstall ----------------------------------------------------------------


# Modules that may legitimately be unavailable on a fresh machine (no agent CLI logged
# in, no GitHub token): they don't fail an install. Everything else must report ok.
OPTIONAL_MODULES = frozenset({"agents", "github"})


def unhealthy_modules(body: object) -> list[str]:
    """Required modules that /healthz reports as not ok (``["?"]`` if unreadable)."""
    modules = body.get("modules") if isinstance(body, dict) else None
    if not isinstance(modules, dict):
        return ["?"]
    return sorted(
        name
        for name, m in modules.items()
        if name not in OPTIONAL_MODULES and not (isinstance(m, dict) and m.get("ok") is True)
    )


def _wait_healthy(sys: System, cfg: Config) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while True:
        try:
            r = httpx.get(f"http://127.0.0.1:{cfg.port}/healthz", timeout=2, trust_env=False)
            if r.status_code == 200 and not unhealthy_modules(r.json()):
                return True
        except (httpx.HTTPError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        sys.sleep(0.5)


def install(kb_path: Path, sys: System) -> int:
    kb = kb_path.expanduser().resolve()
    uv = sys.which("uv")
    if uv is None:
        sys.echo("error: uv is not on PATH (brew install uv)")
        return 1
    path = config_path()
    if sys.dry_run and not path.exists():
        sys.echo(f"would create: {path} (mode 600, new token)")
        cfg = Config(token="", data_dir=path.parent, log_dir=default_log_dir())
    else:
        cfg = load_config(path)  # creates it, with a token, on first install
    cfg = replace(cfg, kb_path=kb)
    sys.mkdir(kb)
    if sys.dry_run:
        sys.echo(f"would set kb.path = {kb} in {path}")
    else:
        save_config(cfg, path)
    sys.mkdir(cfg.data_dir)
    sys.mkdir(cfg.log_dir)

    extra = {"HOME": str(Path.home())}
    if ENV_HOME in sys.environ:
        extra[ENV_HOME] = sys.environ[ENV_HOME]
    plist = render_plist(
        Path(uv), REPO, cfg.log_dir, sys.environ.get("PATH", "/usr/bin:/bin"),
        sys.environ.get("SSH_AUTH_SOCK"), extra,
    )  # fmt: skip
    sys.write(plist_path(), plist)
    sys.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False)
    sys.run(["launchctl", "bootstrap", _domain(), str(plist_path())])
    if sys.dry_run:
        sys.echo(f"would wait up to {HEALTH_TIMEOUT:.0f}s for http://127.0.0.1:{cfg.port}/healthz")
    elif not _wait_healthy(sys, cfg):
        sys.echo(f"error: the server did not become healthy; see {cfg.log_dir}")
        return 1

    for name, register in (("claude", register_claude), ("codex", register_codex)):
        binary = sys.which(name)
        label = "Claude Code" if name == "claude" else "Codex"
        if binary and sys.ask(f"Register Jesinia's MCP tools with {label}?"):
            register(sys, cfg, binary)
            if not sys.dry_run:
                sys.echo(f"{label}: registered")

    if sys.dry_run:
        sys.echo("would print a one-time login link")
        return 0
    try:
        login = call(cfg, "POST", "/api/auth/nonce")
    except ServerDown:  # pragma: no cover - it was healthy a moment ago
        return 1
    sys.echo(f"Jesinia is running. Open: {login['url']}")
    return 0


def uninstall(sys: System, *, purge_data: bool) -> int:
    sys.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False)
    sys.remove(plist_path())
    unregister(sys)
    if not purge_data:
        sys.echo(f"kept data in {default_data_dir()} (use --purge-data to remove it)")
        return 0
    data, logs = default_data_dir(), default_log_dir()
    kb: Path | None = None
    if config_path().exists():
        kb = load_config(config_path()).kb_path
    if kb is not None and any(
        kb.resolve().is_relative_to(d.resolve()) or d.resolve().is_relative_to(kb.resolve())
        for d in (data, logs)
    ):
        sys.echo(f"error: the KB ({kb}) overlaps {data} or {logs}; remove data by hand")
        return 1
    if not sys.ask(f"Delete {data} and {logs}? The KB folder is not touched."):
        sys.echo("kept data")
        return 0
    sys.remove(data)
    sys.remove(logs)
    return 0
