# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detect sync mode and problems without changing the repo or using the network."""

from __future__ import annotations

from pathlib import Path

from jesinia.services.kbsync.application.ports import Git
from jesinia.services.kbsync.domain.state import GitState, Mode, Problem


async def _git_path(git: Git, name: str) -> Path:
    r = await git.run("rev-parse", "--path-format=absolute", "--git-path", name)
    return Path(r.out)


async def detect(git: Git | None, root: Path) -> GitState:
    off = GitState("off", None, None, 0, 0, False, None)
    if git is None or not root.is_dir():
        return off
    inside = await git.run("rev-parse", "--is-inside-work-tree", check=False)
    top = await git.run("rev-parse", "--show-toplevel", check=False)
    if inside.code != 0 or inside.out != "true" or Path(top.out).resolve() != root.resolve():
        return off

    head = await git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = head.out if head.code == 0 else None
    problem: Problem | None = None
    if (await _git_path(git, "rebase-merge")).exists() or (
        await _git_path(git, "rebase-apply")
    ).exists():
        problem = "rebase-in-progress"
    elif (await _git_path(git, "MERGE_HEAD")).exists():
        problem = "merge-in-progress"
    elif branch is None:
        problem = "detached-head"

    remotes = (await git.run("remote")).out.split()
    up = await git.run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    upstream = up.out if up.code == 0 and up.out else None
    mode: Mode = "push" if remotes and upstream else "commit"
    if remotes and upstream is None and problem is None:
        problem = "no-upstream"

    ahead = behind = 0
    if upstream is not None:
        counts = await git.run("rev-list", "--left-right", "--count", "@{u}...HEAD", check=False)
        if counts.code == 0:
            behind_s, ahead_s = counts.out.split()
            behind, ahead = int(behind_s), int(ahead_s)
    dirty = bool((await git.run("status", "--porcelain", "--untracked-files=normal")).out)
    return GitState(mode, branch, upstream, ahead, behind, dirty, problem)
