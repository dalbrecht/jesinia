# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo workspace against real git: a bare "GitHub" remote in tmp_path, a mirror,
detached worktrees per head, and per-file patch IDs for rebase detection."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jesinia.apps.review.application.ports import Workspace
from jesinia.apps.review.domain.workspace import (
    GitCommandError,
    GitOutputError,
    GitTimeoutError,
    GitUnavailableError,
    InvalidRefError,
    InvalidRepoError,
    InvalidShaError,
    WorkspaceError,
    WorkspacePathError,
    canonical_repo,
    check_ref,
    check_sha,
    parse_repo,
)
from jesinia.apps.review.infrastructure import workspace as workspace_module
from jesinia.apps.review.infrastructure.workspace import (
    EMPTY_TREE,
    GitWorkspace,
    github_remote_url,
)

REPO = "acme/widget"
SPAWN = "jesinia.apps.review.infrastructure.workspace.spawn"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
INHERITED = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)
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
    assert (root / ".hidden" / "x" / "trees" / ("a" * 40)).is_dir()


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


async def test_git_env_is_non_interactive_and_isolated(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def recording_spawn(*argv: str, **kwargs: Any) -> Any:
        seen.append(kwargs["env"])
        return await real_spawn(*argv, **kwargs)

    # The system config stays in force (it holds macOS's osxkeychain credential helper);
    # a git hook's repository variables must not redirect commands to another repo.
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    elsewhere = tmp_path / "elsewhere"
    for key in INHERITED:
        monkeypatch.setenv(key, str(elsewhere))
    monkeypatch.setattr(SPAWN, recording_spawn)
    ws = make_workspace(tmp_path, fixture)
    await ws.ensure_mirror(REPO)
    await ws.fetch(REPO, ["refs/heads/main"])
    assert not elsewhere.exists()
    root = str((tmp_path / "data" / "review" / "repos").resolve())
    assert seen
    for env in seen:
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_PAGER"] == "cat"
        assert env["GIT_LFS_SKIP_SMUDGE"] == "1" and env["GIT_CEILING_DIRECTORIES"] == root
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert not set(INHERITED) & set(env)


async def test_worktree_ignores_hostile_global_lfs_config(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "lfs-work"
    subprocess.run(["git", "clone", "-q", str(fixture.remote), str(work)], check=True)
    git(work, "checkout", "-q", "-b", "lfs", fixture.base)
    (work / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (work / "big.bin").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:00\n")
    lfs_head = commit_all(work, "lfs")
    git(work, "push", "-q", "origin", "lfs")
    # A global config like the author's: LFS required, but its binary is missing.
    home = tmp_path / "home"
    home.mkdir()
    hostile = home / ".gitconfig"
    hostile.write_text(
        '[filter "lfs"]\n\trequired = true\n\tsmudge = git-lfs-missing smudge %f\n'
        "\tprocess = git-lfs-missing filter-process\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    ws = make_workspace(tmp_path, fixture)
    await ws.ensure_mirror(REPO)
    await ws.fetch(REPO, ["refs/heads/lfs"])
    tree = await ws.worktree(REPO, lfs_head)
    assert (tree / "big.bin").read_text().startswith("version https://git-lfs")


async def test_repo_names_are_case_insensitive(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    kept = await ws.worktree(REPO, fixture.head)
    assert await ws.worktree("Acme/Widget", fixture.head) == kept
    assert ws._lock("Acme/Widget") is ws._lock(REPO)
    assert await ws.prune({("Acme/Widget", fixture.head)}) == 0
    assert kept.is_dir()


async def test_worktree_recovers_when_its_directory_was_deleted(
    tmp_path: Path, fixture: Fixture
) -> None:
    ws = await fetched(tmp_path, fixture)
    path = await ws.worktree(REPO, fixture.head)
    shutil.rmtree(path)  # still registered in the mirror
    assert await ws.worktree(REPO, fixture.head) == path
    assert (path / "c.txt").read_text() == "new file\n"


async def test_an_interrupted_add_never_yields_a_partial_tree(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = await fetched(tmp_path, fixture)
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def add_then_hang(*argv: str, **kwargs: Any) -> Any:
        if "add" in argv:
            # git registered and checked out the tree, then got stuck: a partial checkout.
            subprocess.run(argv, check=True, capture_output=True, env=kwargs["env"])
            target = Path(argv[argv.index("--detach") + 1])
            (target / "c.txt").unlink()
            return await real_spawn("sleep", "30", **kwargs)
        return await real_spawn(*argv, **kwargs)

    monkeypatch.setattr(SPAWN, add_then_hang)
    hanging = GitWorkspace(ws.root, lambda repo: str(fixture.remote), command_timeout=0.5)
    with pytest.raises(GitTimeoutError):
        await hanging.worktree(REPO, fixture.head)
    trees = ws.root.resolve() / "acme" / "widget" / "trees"
    assert not (trees / fixture.head).exists()
    assert [p.name for p in trees.iterdir()] == []

    monkeypatch.setattr(SPAWN, real_spawn)
    path = await ws.worktree(REPO, fixture.head)
    assert (path / "c.txt").read_text() == "new file\n"


async def test_a_timed_out_git_is_killed_and_reported(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[asyncio.subprocess.Process] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def hang(*argv: str, **kwargs: Any) -> Any:
        proc = await real_spawn("sleep", "30", **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(SPAWN, hang)
    ws = GitWorkspace(tmp_path / "repos", str, command_timeout=0.2, fetch_timeout=0.3)
    with pytest.raises(GitTimeoutError, match=r"init .* timed out after 0.2s"):
        await ws.ensure_mirror(REPO)
    with pytest.raises(GitTimeoutError, match=r"fetch .* timed out after 0.3s") as caught:
        await ws.fetch(REPO, ["refs/heads/main"])
    assert "fetch" in caught.value.argv and caught.value.seconds == 0.3
    assert all(proc.returncode is not None for proc in started)


async def test_a_cancelled_git_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[asyncio.subprocess.Process] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def hang(*argv: str, **kwargs: Any) -> Any:
        proc = await real_spawn("sleep", "30", **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(SPAWN, hang)
    task = asyncio.create_task(GitWorkspace(tmp_path / "repos", str).ensure_mirror(REPO))
    while not started:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started[0].returncode is not None


async def test_a_missing_git_binary_is_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    with pytest.raises(GitUnavailableError) as caught:
        await GitWorkspace(tmp_path / "repos", str).ensure_mirror(REPO)
    assert isinstance(caught.value.__cause__, OSError)


async def test_fetch_with_no_refs_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*argv: str, **kwargs: Any) -> Any:
        raise AssertionError(f"ran {argv}")

    monkeypatch.setattr(SPAWN, refuse)
    await GitWorkspace(tmp_path / "repos", str).fetch(REPO, [])


async def test_diff_ignores_the_users_diff_config(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = await fetched(tmp_path, fixture)
    plain = await ws.diff(REPO, fixture.base, fixture.head)
    ids = await ws.patch_ids(REPO, fixture.base, fixture.head)
    custom = tmp_path / "custom-gitconfig"
    custom.write_text(
        "[diff]\n\tnoprefix = true\n\tcontext = 9\n\tmnemonicPrefix = true\n"
        "\talgorithm = histogram\n\trenames = false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(custom))
    assert await ws.diff(REPO, fixture.base, fixture.head) == plain
    assert await ws.patch_ids(REPO, fixture.base, fixture.head) == ids
    assert "--- a/a.txt\n+++ b/a.txt" in plain


async def test_patch_ids_cover_renames_deletes_and_binaries(
    tmp_path: Path, fixture: Fixture
) -> None:
    work = tmp_path / "more-work"
    subprocess.run(["git", "clone", "-q", str(fixture.remote), str(work)], check=True)
    git(work, "checkout", "-q", "-b", "more", fixture.base)
    git(work, "mv", "a.txt", "renamed.txt")
    git(work, "rm", "-q", "b.txt")
    (work / "blob.bin").write_bytes(bytes(range(256)))
    more_head = commit_all(work, "more")
    git(work, "push", "-q", "origin", "more")
    ws = make_workspace(tmp_path, fixture)
    await ws.ensure_mirror(REPO)
    await ws.fetch(REPO, ["refs/heads/more"])
    ids = await ws.patch_ids(REPO, fixture.base, more_head)
    assert set(ids) == {"renamed.txt", "b.txt", "blob.bin"}
    assert len(set(ids.values())) == 3 and all(len(v) == 40 for v in ids.values())


async def test_prune_never_follows_symlinks(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    kept = await ws.worktree(REPO, fixture.head)
    trees = kept.parent
    link = trees / fixture.rebased_head
    link.symlink_to(kept, target_is_directory=True)
    outside = tmp_path / "outside"
    (outside / "x" / "trees" / fixture.head).mkdir(parents=True)
    (ws.root / "evil").symlink_to(outside, target_is_directory=True)
    (trees / f".tmp-{fixture.conflict_head}-stale").mkdir()

    assert await ws.prune({(REPO, fixture.head)}) == 1
    assert not link.is_symlink() and kept.is_dir() and (kept / "c.txt").exists()
    assert (outside / "x" / "trees" / fixture.head).is_dir()
    assert not (trees / f".tmp-{fixture.conflict_head}-stale").exists()


async def test_worktree_replaces_a_symlink_without_following_it(
    tmp_path: Path, fixture: Fixture
) -> None:
    ws = await fetched(tmp_path, fixture)
    kept = await ws.worktree(REPO, fixture.head)
    link = kept.parent / fixture.rebased_head
    link.symlink_to(kept, target_is_directory=True)
    path = await ws.worktree(REPO, fixture.rebased_head)
    assert path == link and not path.is_symlink()
    assert (kept / "c.txt").exists()


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
    assert github_remote_url("Acme/Widget") == "https://github.com/acme/widget.git"
    with pytest.raises(InvalidRepoError):
        github_remote_url("acme/widget/../x")


@pytest.mark.parametrize(
    "repo",
    ["acme", "acme/", "/widget", "acme/widget/x", "acme/..", "acme/.", "-acme/w", "ac me/w", ""],
)
def test_parse_repo_rejects_anything_but_owner_slash_name(repo: str) -> None:
    with pytest.raises(InvalidRepoError):
        parse_repo(repo)


def test_parse_repo_accepts_github_names_case_insensitively() -> None:
    assert parse_repo("Acme-Co/Widget.js_2") == ("acme-co", "widget.js_2")
    assert canonical_repo("Acme/Widget") == "acme/widget"


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


async def test_a_failed_add_leaves_nothing_behind(tmp_path: Path, fixture: Fixture) -> None:
    ws = await fetched(tmp_path, fixture)
    unknown = "b" * 40
    with pytest.raises(GitCommandError, match=unknown):
        await ws.worktree(REPO, unknown)
    trees = ws.root.resolve() / "acme" / "widget" / "trees"
    assert not trees.exists() or [p.name for p in trees.iterdir()] == []


async def test_patch_ids_refuse_a_name_list_that_disagrees_with_the_diff(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = await fetched(tmp_path, fixture)
    real_run = ws._run

    async def phantom_name(location: tuple[str, ...], *args: str, **kwargs: Any) -> bytes:
        out = await real_run(location, *args, **kwargs)
        return out + b"phantom.txt\0" if "--name-only" in args else out

    monkeypatch.setattr(ws, "_run", phantom_name)
    with pytest.raises(GitOutputError, match="3 paths but 2 file diffs"):
        await ws.patch_ids(REPO, fixture.base, fixture.head)


async def test_patch_ids_refuse_a_file_diff_without_a_patch_id(
    tmp_path: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = await fetched(tmp_path, fixture)
    real_git = ws._git

    async def first_line_only(location: tuple[str, ...], *args: str, **kwargs: Any) -> str:
        out = await real_git(location, *args, **kwargs)
        return out.splitlines(keepends=True)[0] if "patch-id" in args else out

    monkeypatch.setattr(ws, "_git", first_line_only)
    with pytest.raises(GitOutputError, match="no patch id for c.txt"):
        await ws.patch_ids(REPO, fixture.base, fixture.head)


async def test_a_broken_tree_inside_another_repo_has_no_head(tmp_path: Path) -> None:
    # Without GIT_CEILING_DIRECTORIES, rev-parse in a damaged tree walks up and reports the
    # HEAD of whatever repository happens to contain the workspace.
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q")
    (outer / "f.txt").write_text("outer\n")
    commit_all(outer, "outer")
    ws = GitWorkspace(outer / "data" / "review" / "repos", str)
    broken = ws.root / "acme" / "widget" / "trees" / ("a" * 40)
    broken.mkdir(parents=True)
    assert git(broken, "rev-parse", "HEAD")  # plain git does find the outer repo
    assert await ws._head_of(broken) is None


def test_empty_tree_is_gits_own() -> None:
    out = subprocess.run(
        ["git", "hash-object", "-t", "tree", "/dev/null"], check=True, capture_output=True
    )
    assert out.stdout.decode().strip() == EMPTY_TREE


async def test_every_repository_command_ignores_in_tree_attributes(
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
    await ws.patch_ids(REPO, fixture.base, fixture.head)
    repository_commands = [argv for argv in recorded if "init" not in argv]
    assert repository_commands
    assert all(f"--attr-source={EMPTY_TREE}" in argv for argv in repository_commands)


async def test_kill_treats_eperm_as_nothing_left_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # macOS answers EPERM for a process group whose only members are zombies.
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec
    proc: asyncio.subprocess.Process = await real_spawn("true", start_new_session=True)
    await asyncio.sleep(0.05)

    def refuse(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("os.killpg", refuse)
    await workspace_module._kill(proc)
    assert proc.returncode == 0


async def test_stderr_is_read_with_a_cap() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"e" * 100_000)
    reader.feed_eof()
    kept = await workspace_module._read_capped(reader, 1000)
    assert kept == b"e" * 1000
    assert reader.at_eof()  # the rest was drained, so git never blocks on a full pipe


async def test_regex_dialect_is_probed_from_the_real_git_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("foo\n")

    def matches(pattern: str) -> bool:
        return (
            subprocess.run(
                ["git", "grep", "--no-index", "-E", "-q", "-e", pattern, "--", "probe.txt"],
                cwd=tmp_path,
                check=False,
            ).returncode
            == 0
        )

    ws = GitWorkspace(tmp_path / "repos", str)
    dialect = await ws.regex_dialect()
    assert dialect.native_word_boundaries == matches("\\bfoo")
    assert dialect.not_word_boundary == (dialect.native_word_boundaries and matches("f\\Boo"))

    async def refuse(*argv: str, **kwargs: Any) -> Any:
        raise AssertionError(f"probed again: {argv}")

    monkeypatch.setattr(SPAWN, refuse)
    assert await ws.regex_dialect() is dialect


async def test_a_failed_regex_probe_is_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def broken_git(*argv: str, **kwargs: Any) -> Any:
        return await real_spawn("sh", "-c", "echo 'regex library missing' >&2; exit 2", **kwargs)

    monkeypatch.setattr(SPAWN, broken_git)
    ws = GitWorkspace(tmp_path / "repos", str)
    with pytest.raises(GitCommandError, match="regex library missing"):
        await ws.regex_dialect()
