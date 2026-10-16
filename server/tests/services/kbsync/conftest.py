# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real git repositories for kbsync tests: a work repo with a local bare remote."""

import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "Notes").mkdir()
    (path / "Notes/a.md").write_text("base\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    return bare


@pytest.fixture
def repo_with_remote(tmp_path: Path, remote: Path) -> Path:
    work = _init(tmp_path / "work")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "-u", "origin", "main")
    return work


@pytest.fixture
def repo_no_remote(tmp_path: Path) -> Path:
    return _init(tmp_path / "solo")


@pytest.fixture
def other_clone(tmp_path: Path, remote: Path, repo_with_remote: Path) -> Path:
    clone = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    return clone
