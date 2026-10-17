# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo workspace against real git: a bare "GitHub" remote in tmp_path, a mirror,
detached worktrees per head, and per-file patch IDs for rebase detection."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jesinia.apps.review.application.ports import Workspace
from jesinia.apps.review.domain.workspace import (
    GitCommandError,
    InvalidRefError,
    InvalidRepoError,
    InvalidShaError,
    WorkspaceError,
    WorkspacePathError,
    check_ref,
    check_sha,
    parse_repo,
)
from jesinia.apps.review.infrastructure.workspace import GitWorkspace, github_remote_url

REPO = "acme/widget"
SPAWN = "jesinia.apps.review.infrastructure.workspace.spawn"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
A_LINES = "".join(f"line {i}\n" for i in range(1, 21))


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(work: Path, message: str) -> str:
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


@dataclass(frozen=True)
class Fixture:
    """SHAs of a PR rebased twice: once cleanly, once over a conflicting change."""

    remote: Path
    base: str  # main when the PR was opened
    head: str  # the PR as approved
    rebased_base: str  # main after an unrelated change
    rebased_head: str  # the PR rebased onto it, no conflicts
    conflict_base: str  # main after a change to the PR's own line
    conflict_head: str  # the PR rebased onto it with a hand-resolved conflict


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "main")
    (work / "a.txt").write_text(A_LINES)
    (work / "b.txt").write_text("b\n")
    base = commit_all(work, "base")

    git(work, "checkout", "-q", "-b", "feature")
    (work / "a.txt").write_text(A_LINES.replace("line 2\n", "line two\n"))
    (work / "c.txt").write_text("new file\n")
    head = commit_all(work, "feature")

    git(work, "checkout", "-q", "-b", "main2", base)
    (work / "b.txt").write_text("b changed\n")
    rebased_base = commit_all(work, "unrelated")
    git(work, "checkout", "-q", "-b", "rebased", head)
    git(work, "rebase", "-q", "main2")
    rebased_head = git(work, "rev-parse", "HEAD")

    git(work, "checkout", "-q", "-b", "main3", base)
    (work / "a.txt").write_text(A_LINES.replace("line 2\n", "line 2 (main)\n"))
    conflict_base = commit_all(work, "conflicting")
    git(work, "checkout", "-q", "-b", "conflict", conflict_base)
    (work / "a.txt").write_text(A_LINES.replace("line 2\n", "line two (main)\n"))
    (work / "c.txt").write_text("new file\n")
    conflict_head = commit_all(work, "feature, resolved")

    for branch in ("main", "feature", "main2", "rebased", "main3", "conflict"):
        git(work, "push", "-q", "origin", branch)
    return Fixture(remote, base, head, rebased_base, rebased_head, conflict_base, conflict_head)


def make_workspace(tmp_path: Path, fixture: Fixture) -> GitWorkspace:
    return GitWorkspace(tmp_path / "data" / "review" / "repos", lambda repo: str(fixture.remote))


def test_git_workspace_satisfies_the_port(tmp_path: Path) -> None:
    port: Workspace = GitWorkspace(tmp_path, github_remote_url)  # checked by mypy
    assert port is not None


async def fetched(tmp_path: Path, fixture: Fixture) -> GitWorkspace:
    ws = make_workspace(tmp_path, fixture)
    await ws.ensure_mirror(REPO)
    await ws.fetch(REPO, ["refs/heads/main", "refs/heads/feature", "refs/heads/rebased"])
    await ws.fetch(REPO, ["refs/heads/main2", "refs/heads/main3", fixture.conflict_head])
    return ws


async def test_worktree_is_idempotent_and_detached(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    first = await ws.worktree(REPO, fixture.head)
    again = await ws.worktree(REPO, fixture.head)
    assert first == again
    assert first.is_relative_to((tmp_path / "data" / "review" / "repos").resolve())
    assert (first / "c.txt").read_text() == "new file\n"
    assert git(first, "rev-parse", "HEAD") == fixture.head
    # Detached: HEAD names no branch.
    assert git(first, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    listed = git(first, "worktree", "list", "--porcelain")
    assert listed.count(f"worktree {first}") == 1


async def test_worktree_recreates_a_damaged_checkout(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    path = await ws.worktree(REPO, fixture.head)
    (path / ".git").unlink()  # no longer a worktree: rev-parse fails inside it
    assert await ws.worktree(REPO, fixture.head) == path
    assert git(path, "rev-parse", "HEAD") == fixture.head


async def test_patch_ids_equal_for_pure_rebase_and_differ_with_conflict_edit(
    tmp_path: Path, fixture: Fixture
) -> None:
    ws = await fetched(tmp_path, fixture)
    approved = await ws.patch_ids(REPO, fixture.base, fixture.head)
    rebased = await ws.patch_ids(REPO, fixture.rebased_base, fixture.rebased_head)
    conflicted = await ws.patch_ids(REPO, fixture.conflict_base, fixture.conflict_head)

    assert set(approved) == {"a.txt", "c.txt"}
    assert all(len(patch_id) == 40 for patch_id in approved.values())
    assert rebased == approved
    assert set(conflicted) == {"a.txt", "c.txt"}
    assert conflicted["c.txt"] == approved["c.txt"]
    assert conflicted["a.txt"] != approved["a.txt"]


async def test_diff_and_merge_base(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    assert await ws.merge_base(REPO, fixture.rebased_head, fixture.conflict_head) == fixture.base
    diff = await ws.diff(REPO, fixture.base, fixture.head)
    assert "+line two" in diff and "+new file" in diff and "b.txt" not in diff


async def test_prune_removes_unkept_worktrees(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    kept = await ws.worktree(REPO, fixture.head)
    dropped = await ws.worktree(REPO, fixture.rebased_head)
    also_dropped = await ws.worktree(REPO, fixture.conflict_head)

    assert await ws.prune({(REPO, fixture.head)}) == 2
    assert kept.is_dir() and not dropped.exists() and not also_dropped.exists()
    listed = git(kept, "worktree", "list", "--porcelain")
    assert str(dropped) not in listed and str(kept) in listed
    assert await ws.prune({(REPO, fixture.head)}) == 0


async def test_prune_with_nothing_on_disk_is_a_no_op(tmp_path: Path, fixture: Fixture) -> None:
    assert await make_workspace(tmp_path, fixture).prune(set()) == 0


async def test_prune_skips_entries_it_did_not_create(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    root = tmp_path / "data" / "review" / "repos"
    (root / "stray-file").write_text("x")
    (root / "acme" / "stray-file").write_text("x")
    (root / "acme" / "widget" / "trees" / "not-a-sha").mkdir(parents=True)
    (root / ".hidden" / "x" / "trees" / ("a" * 40)).mkdir(parents=True)
    assert await ws.prune(set()) == 0
    assert (root / "acme" / "widget" / "trees" / "not-a-sha").is_dir()


async def test_never_pushes(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[str, ...]] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def recording_spawn(*argv: str, **kwargs: Any) -> Any:
        recorded.append(argv)
        return await real_spawn(*argv, **kwargs)

    monkeypatch.setattr(SPAWN, recording_spawn)
    ws = await fetched(tmp_path, fixture)
    await ws.worktree(REPO, fixture.head)
    await ws.diff(REPO, fixture.base, fixture.head)
    await ws.merge_base(REPO, fixture.base, fixture.head)
    await ws.patch_ids(REPO, fixture.base, fixture.head)
    await ws.prune(set())

    assert recorded, "the workspace ran no git commands"
    assert all(argv[0] == "git" for argv in recorded)
    assert not any("push" in argv for argv in recorded)


async def test_git_runs_non_interactively_without_system_config(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def recording_spawn(*argv: str, **kwargs: Any) -> Any:
        seen.append(kwargs["env"])
        return await real_spawn(*argv, **kwargs)

    monkeypatch.setattr(SPAWN, recording_spawn)
    await make_workspace(tmp_path, fixture).ensure_mirror(REPO)
    assert seen and all(
        env["GIT_TERMINAL_PROMPT"] == "0"
        and env["GIT_CONFIG_NOSYSTEM"] == "1"
        and env["GIT_PAGER"] == "cat"
        for env in seen
    )


async def test_failing_git_surfaces_its_stderr(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    missing = "0" * 40
    with pytest.raises(GitCommandError) as caught:
        await ws.diff(REPO, missing, fixture.head)
    assert caught.value.code != 0
    assert missing in caught.value.stderr
    assert "git" in str(caught.value) and caught.value.stderr.strip() in str(caught.value)
    assert isinstance(caught.value, WorkspaceError)


async def test_fetch_of_an_unknown_ref_fails_with_cause(tmp_path: Path, fixture: Fixture) -> None:
    ws = make_workspace(tmp_path, fixture)
    await ws.ensure_mirror(REPO)
    with pytest.raises(GitCommandError, match="refs/heads/nope"):
        await ws.fetch(REPO, ["refs/heads/nope"])


async def test_ensure_mirror_is_idempotent(tmp_path: Path, fixture: Fixture) -> None:
    ws = make_workspace(tmp_path, fixture)
    await ws.ensure_mirror(REPO)
    await ws.ensure_mirror(REPO)
    mirror = tmp_path / "data" / "review" / "repos" / "acme" / "widget" / "mirror.git"
    assert git(mirror, "rev-parse", "--is-bare-repository") == "true"
    assert git(mirror, "config", "remote.origin.url") == str(fixture.remote)


async def test_inputs_are_validated_before_git_runs(tmp_path: Path, fixture: Fixture) -> None:
    ws = make_workspace(tmp_path, fixture)
    with pytest.raises(InvalidRepoError):
        await ws.ensure_mirror("../etc")
    with pytest.raises(InvalidShaError):
        await ws.worktree(REPO, "HEAD")
    with pytest.raises(InvalidShaError):
        await ws.diff(REPO, "--output=/tmp/x", "a" * 40)
    with pytest.raises(InvalidRefError):
        await ws.fetch(REPO, ["--upload-pack=touch /tmp/pwned"])
    with pytest.raises(InvalidRepoError):
        await ws.prune({("not a repo", "a" * 40)})
    assert not (tmp_path / "data").exists()


async def test_workspace_paths_stay_inside_the_root(tmp_path: Path, fixture: Fixture) -> None:
    root = tmp_path / "data" / "review" / "repos"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "acme").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        await GitWorkspace(root, lambda repo: str(fixture.remote)).ensure_mirror(REPO)
    assert list(outside.iterdir()) == []


async def test_concurrent_worktree_calls_share_one_checkout(
    tmp_path: Path, fixture: Fixture
) -> None:
    ws = await fetched(tmp_path, fixture)
    paths = await asyncio.gather(*(ws.worktree(REPO, fixture.head) for _ in range(3)))
    assert len(set(paths)) == 1


def test_github_remote_url_uses_https_and_no_credentials() -> None:
    assert github_remote_url("acme/widget") == "https://github.com/acme/widget.git"
    with pytest.raises(InvalidRepoError):
        github_remote_url("acme/widget/../x")


@pytest.mark.parametrize(
    "repo",
    ["acme", "acme/", "/widget", "acme/widget/x", "acme/..", "acme/.", "-acme/w", "ac me/w", ""],
)
def test_parse_repo_rejects_anything_but_owner_slash_name(repo: str) -> None:
    with pytest.raises(InvalidRepoError):
        parse_repo(repo)


def test_parse_repo_accepts_github_names() -> None:
    assert parse_repo("acme-co/widget.js_2") == ("acme-co", "widget.js_2")


@pytest.mark.parametrize("sha", ["", "abc123", "A" * 40, "g" * 40, "a" * 41, "-" + "a" * 39])
def test_check_sha_requires_a_full_lowercase_sha(sha: str) -> None:
    with pytest.raises(InvalidShaError):
        check_sha(sha)


def test_check_sha_accepts_a_full_sha() -> None:
    assert check_sha("0123456789abcdef" * 2 + "01234567") == "0123456789abcdef" * 2 + "01234567"


@pytest.mark.parametrize(
    "ref", ["main", "-x", "refs/heads/../x", "refs/heads/a b", "refs/heads/x:y", "refs/", ""]
)
def test_check_ref_rejects_unsafe_refs(ref: str) -> None:
    with pytest.raises(InvalidRefError):
        check_ref(ref)


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/pull/12/head", "a" * 40])
def test_check_ref_accepts_full_refs_and_shas(ref: str) -> None:
    assert check_ref(ref) == ref
