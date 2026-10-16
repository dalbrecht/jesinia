# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import pytest

from jesinia.services.kbsync.application.detect import detect
from jesinia.services.kbsync.infrastructure.git import GitError, GitRunner
from tests.services.kbsync.conftest import git


async def test_modes(tmp_path: Path, repo_with_remote: Path, repo_no_remote: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert (await detect(None, tmp_path)).mode == "off"
    assert (await detect(GitRunner(plain), plain)).mode == "off"
    solo = await detect(GitRunner(repo_no_remote), repo_no_remote)
    assert solo.mode == "commit" and solo.problem is None and solo.branch == "main"
    pushy = await detect(GitRunner(repo_with_remote), repo_with_remote)
    assert pushy.mode == "push" and pushy.upstream == "origin/main" and pushy.problem is None
    assert (pushy.ahead, pushy.behind, pushy.dirty) == (0, 0, False)


async def test_dirty_and_ahead(repo_with_remote: Path) -> None:
    (repo_with_remote / "Notes/a.md").write_text("changed\n")
    st = await detect(GitRunner(repo_with_remote), repo_with_remote)
    assert st.dirty
    git(repo_with_remote, "commit", "-qam", "local")
    st = await detect(GitRunner(repo_with_remote), repo_with_remote)
    assert st.ahead == 1 and not st.dirty


async def test_detached_head_reported(repo_with_remote: Path) -> None:
    await GitRunner(repo_with_remote).run("checkout", "-q", "--detach")
    st = await detect(GitRunner(repo_with_remote), repo_with_remote)
    assert st.problem == "detached-head" and st.branch is None


async def test_no_upstream_with_remote(repo_with_remote: Path) -> None:
    git(repo_with_remote, "branch", "--unset-upstream")
    st = await detect(GitRunner(repo_with_remote), repo_with_remote)
    assert st.mode == "commit" and st.problem == "no-upstream"


async def test_rebase_and_merge_in_progress(repo_with_remote: Path) -> None:
    gitdir = repo_with_remote / ".git"
    (gitdir / "rebase-merge").mkdir()
    assert (
        await detect(GitRunner(repo_with_remote), repo_with_remote)
    ).problem == "rebase-in-progress"
    (gitdir / "rebase-merge").rmdir()
    (gitdir / "MERGE_HEAD").write_text(git(repo_with_remote, "rev-parse", "HEAD"))
    assert (
        await detect(GitRunner(repo_with_remote), repo_with_remote)
    ).problem == "merge-in-progress"


async def test_runner_errors_and_env(repo_no_remote: Path) -> None:
    runner = GitRunner(repo_no_remote)
    with pytest.raises(GitError) as exc:
        await runner.run("not-a-command")
    assert exc.value.result.code != 0 and "not-a-command" in str(exc.value)
    r = await runner.run("not-a-command", check=False)
    assert r.code != 0
    assert (await runner.run("rev-parse", "--is-inside-work-tree")).out == "true"
