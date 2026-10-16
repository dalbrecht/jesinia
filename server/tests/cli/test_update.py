# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import respx
from httpx import ConnectError, Response
from typer.testing import CliRunner

from jesinia.cli import update as upd
from jesinia.cli.install import System
from jesinia.core.config import Config


@dataclass
class Git(System):
    """Scripted git/make/launchctl: answers keyed by a substring of the command."""

    answers: dict[str, tuple[int, str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    out: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.echo = self.out.append
        self.sleep = lambda s: None

    def run(self, argv: list[str], *, check: bool = True) -> tuple[int, str]:
        cmd = " ".join(argv)
        self.calls.append(cmd)
        for key, answer in self.answers.items():
            if key in cmd:
                if check and answer[0] != 0:
                    raise RuntimeError(cmd)
                return answer
        return 0, ""


BASE = {
    "rev-parse --abbrev-ref": (0, "main"),
    "status --porcelain": (0, ""),
    "rev-parse HEAD": (0, "aaaaaaaa1111"),
    "rev-parse origin/main": (0, "bbbbbbbb2222"),
}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(token="t", data_dir=tmp_path / "data", log_dir=tmp_path / "logs", port=5374)


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upd, "_healthy", lambda cfg, sleep: True)


def run(cfg: Config, answers: dict[str, tuple[int, str]], **kw: object) -> tuple[str, Git]:
    git = Git(answers={**BASE, **answers})
    return upd.update(cfg, git, Path("/r"), **kw), git  # type: ignore[arg-type]


def test_updates_builds_and_restarts(cfg: Config, healthy: None) -> None:
    result, git = run(cfg, {})
    assert result == "updated aaaaaaaa → bbbbbbbb"
    order = [c.split()[3] if c.startswith("git") else c.split()[0] for c in git.calls]
    assert order.index("merge") < [c.startswith("make") for c in git.calls].index(True) + 1
    assert any(c.startswith("launchctl kickstart -k gui/") for c in git.calls)
    assert git.out[-1].endswith("updated aaaaaaaa → bbbbbbbb")


@pytest.mark.parametrize(
    ("answers", "why"),
    [
        ({"rev-parse --abbrev-ref": (0, "feature")}, "is not on main (feature)"),
        ({"rev-parse --abbrev-ref": (128, "")}, "is not on main (no branch)"),
        ({"status --porcelain": (0, " M x")}, "local changes"),
        ({"fetch": (1, "")}, "could not fetch"),
        ({"rev-parse origin/main": (0, "aaaaaaaa1111")}, "up to date"),
        ({"merge-base": (1, "")}, "diverged"),
    ],
)
def test_skips_safely(cfg: Config, answers: dict[str, tuple[int, str]], why: str) -> None:
    result, git = run(cfg, answers)
    assert why in result and not any(" merge " in c for c in git.calls)


def test_build_failure_rolls_back(cfg: Config, healthy: None) -> None:
    result, git = run(cfg, {"make": (2, "")})
    assert result == "failed: bbbbbbbb did not build cleanly; rolled back to aaaaaaaa"
    assert "git -C /r reset --hard --quiet aaaaaaaa1111" in git.calls


def test_unhealthy_restart_rolls_back(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    results = iter([False, True])
    monkeypatch.setattr(upd, "_healthy", lambda cfg, sleep: next(results))
    result, git = run(cfg, {})
    assert "did not restart cleanly" in result
    assert sum(c.startswith("launchctl kickstart") for c in git.calls) == 2


def test_without_restart(cfg: Config) -> None:
    result, git = run(cfg, {}, restart=False)
    assert result.startswith("updated") and not any("kickstart" in c for c in git.calls)


def test_concurrent_runs_are_skipped(cfg: Config) -> None:
    import fcntl

    cfg.data_dir.mkdir(parents=True)
    with (cfg.data_dir / "update.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result, _ = run(cfg, {})
    assert result == "skipped: another update is running"


def test_health_wait(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    with respx.mock:
        respx.get("http://127.0.0.1:5374/healthz").mock(
            side_effect=[ConnectError("x"), Response(200)]
        )
        assert upd._healthy(cfg, lambda s: None) is True
    monkeypatch.setattr(upd, "HEALTH_TIMEOUT", 0.0)
    assert upd._healthy(cfg, lambda s: None) is False


def test_cli_update_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jesinia.cli import main

    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(upd, "update", lambda cfg, sys: "up to date")
    assert CliRunner().invoke(main.app, ["update"]).exit_code == 0
    monkeypatch.setattr(upd, "update", lambda cfg, sys: "failed: x")
    assert CliRunner().invoke(main.app, ["update"]).exit_code == 1


def test_refuses_a_changed_remote(cfg: Config, healthy: None) -> None:
    git = Git(answers={**BASE, "remote get-url": (0, "https://evil.example/fork.git")})
    git.environ = {"JESINIA_UPDATE_REMOTE": "https://github.com/dalbrecht/jesinia.git"}
    result = upd.update(cfg, git, Path("/r"))
    assert "origin is not the remote" in result and not any(" fetch " in c for c in git.calls)
    git2 = Git(answers={**BASE, "remote get-url": (0, "https://github.com/dalbrecht/jesinia.git")})
    git2.environ = {"JESINIA_UPDATE_REMOTE": "https://github.com/dalbrecht/jesinia.git"}
    assert upd.update(cfg, git2, Path("/r")).startswith("updated")
