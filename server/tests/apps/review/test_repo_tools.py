# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worktree-scoped repo tools: review agents read PR code only through these.

A real worktree, checked out by ``GitWorkspace`` from a bare "GitHub" remote in tmp, holds
ordinary source, auto-skipped files, a symlink to ``/etc``, a symlink to a secret outside
the tree and a symlink that stays inside it.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jesinia.apps.review.api.tools import RepoGitIn, RepoGrepIn, RepoLsIn, RepoReadIn, build_tools
from jesinia.apps.review.application.ports import TreeGit
from jesinia.apps.review.application.repo_tools import (
    SEARCH_TIMEOUT,
    RepoTools,
    _parse_grep,
    safe_path,
)
from jesinia.apps.review.application.sessions import ReviewSessionRegistry
from jesinia.apps.review.domain.repo_access import (
    BSD_REGEX,
    FILE_LINES_PER_SESSION,
    GNU_REGEX,
    GREP_MAX_LINE_CHARS,
    GREP_MAX_MATCHES,
    READ_LINES_PER_CALL,
    READ_MAX_BYTES,
    AllowanceExhaustedError,
    AutoSkippedFileError,
    CappedOutput,
    FileLineAllowance,
    InvalidSearchError,
    NoReviewSessionError,
    NotACommitError,
    OutsidePrHistoryError,
    RegexDialect,
    RepoGitError,
    RepoPathNotFoundError,
    RepoToolError,
    UnsafeGitArgumentError,
    UnsafePathError,
    check_glob,
    check_pattern,
    check_revision,
    normalize_relative_path,
    plan_git_command,
    posix_ere,
    regex_problem,
)
from jesinia.apps.review.domain.workspace import GitCommandError
from jesinia.apps.review.infrastructure.workspace import EMPTY_TREE, GitWorkspace
from jesinia.apps.review.module import ReviewModule
from jesinia.core.config import Config
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext, CallContext, ToolDef
from jesinia.core.scheduler import Scheduler

REPO = "acme/widget"
SESSION = "review-session-1"
SPAWN = "jesinia.apps.review.infrastructure.workspace.spawn"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
SECRET = "top secret outside the tree"


def _set_git_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    empty = home / "empty-gitconfig"
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
class Tree:
    workspace: GitWorkspace
    path: Path
    first: str
    head: str
    outside: Path
    other: str  # another PR's head, branched from `first`
    other_tree: Path


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tree]:
    base = tmp_path_factory.mktemp("repo-tools")
    with pytest.MonkeyPatch.context() as monkeypatch:
        _set_git_env(monkeypatch, base)
        outside = base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text(f"needle {SECRET}\n")
        remote = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        work = base / "work"
        subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
        git(work, "checkout", "-q", "-b", "main")
        (work / "src").mkdir()
        (work / "src" / "app.py").write_text("def hello():\n    return 'needle'\n")
        (work / "old.txt").write_text("old content\n")
        first = commit_all(work, "first")

        (work / "old.txt").unlink()
        # The PR tries to hide two source files from search and from diffs.
        (work / ".gitattributes").write_text("hidden.py -diff\nhidden2.py binary\n")
        (work / "hidden.py").write_text("secretive_marker = 1\n")
        (work / "hidden2.py").write_text("secretive_marker = 2\n")
        (work / "caf\u00e9.txt").write_text("accent\n")  # NFC spelling on disk
        (work / "evil\nsrc").mkdir()
        (work / "evil\nsrc" / "x.py").write_text("newline_marker\n")
        (work / "alias.txt").symlink_to("big.txt")
        # A NUL past git's 8000-byte binary sniff, in a line that matches: text, to git.
        (work / "aaa-nul.txt").write_bytes(b"pad\n" * 2500 + b"nul_marker\0tail\n")
        (work / "zzz-normal.txt").write_text("nul_marker ok\n")
        # The PR renames its author in blame output, if blame honours this.
        (work / ".mailmap").write_text("Mallory <m@example.invalid> Test <test@example.invalid>\n")

        (work / "src" / "app.py").write_text("def hello():\n    return 'needle v2'\n")
        (work / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 5001)))
        (work / "many.txt").write_text("".join(f"needle {i}\n" for i in range(1, 301)))
        (work / "long.txt").write_text("needle " + "x" * 1000 + "\n")
        (work / "pnpm-lock.yaml").write_text("needle: lock\n")
        (work / "vendor").mkdir()
        (work / "vendor" / "lib.py").write_text("needle = 'vendored'\n")
        (work / "dist").mkdir()
        (work / "dist" / "bundle.js").write_text("needle();\n")
        (work / "image.png").write_bytes(b"\x89PNG\0\0needle\0")
        (work / "docs").mkdir()
        (work / "docs" / "a b:c.md").write_text("first\nneedle in docs\n")
        (work / "etc").symlink_to("/etc")
        (work / "secret-link").symlink_to(outside / "secret.txt")
        (work / "relative-escape").symlink_to("../../../../../../../../outside/secret.txt")
        (work / "inner").symlink_to("src")
        (work / "gitlink").symlink_to(".git")
        head = commit_all(work, "second")
        # main moves on after the PR's head, and another PR branches from `first`.
        (work / "later.txt").write_text("later\n")
        commit_all(work, "later on main")
        git(work, "checkout", "-q", "-b", "other", first)
        (work / "other.txt").write_text("other PR\n")
        other = commit_all(work, "other PR")
        git(work, "push", "-q", "origin", "main", "other")

        workspace = GitWorkspace(base / "data" / "review" / "repos", lambda repo: str(remote))

        async def checkout() -> tuple[Path, Path]:
            await workspace.ensure_mirror(REPO)
            await workspace.fetch(REPO, ["refs/heads/main", "refs/heads/other"])
            return await workspace.worktree(REPO, head), await workspace.worktree(REPO, other)

        path, other_tree = asyncio.run(checkout())
        yield Tree(workspace, path, first, head, outside, other, other_tree)


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_git_env(monkeypatch, tmp_path)


@pytest.fixture
def registry(tree: Tree) -> ReviewSessionRegistry:
    sessions = ReviewSessionRegistry()
    sessions.bind(SESSION, tree.path)
    return sessions


@pytest.fixture
def tools(tree: Tree, registry: ReviewSessionRegistry) -> RepoTools:
    return RepoTools(registry.worktree_for, tree.workspace, registry.allowance)


# --- Review Focus 5: path escapes -------------------------------------------------------


async def test_repo_tools_refuse_escapes_and_symlinks(tools: RepoTools, tree: Tree) -> None:
    escapes = [
        "../../.ssh/id_rsa",
        "src/../../outside/secret.txt",
        "/etc/passwd",
        str(tree.outside / "secret.txt"),
        "etc/passwd",
        "etc",
        "secret-link",
        "relative-escape",
        "gitlink",
        ".git",
        "src/.GIT/config",
        "src/\0app.py",
        "",
    ]
    for path in escapes:
        with pytest.raises(UnsafePathError):
            await tools.read(SESSION, path)
        with pytest.raises(UnsafePathError):
            await tools.ls(SESSION, path or "..")
    for args in (["HEAD:secret-link"], ["HEAD:../x"], ["--", "/etc"]):
        with pytest.raises((UnsafePathError, UnsafeGitArgumentError)):
            await tools.git(SESSION, "show", args)
    with pytest.raises(UnsafePathError):
        await tools.git(SESSION, "blame", ["secret-link"])

    # Nothing outside the tree is ever returned, even by a search for its content.
    found = await tools.grep(SESSION, "top secret")
    assert found["matches"] == []
    listing = await tools.ls(SESSION, ".")
    names = {entry["name"]: entry for entry in listing["entries"]}
    assert ".git" not in names
    assert names["etc"]["kind"] == "symlink" and names["etc"]["inside"] is False
    assert names["inner"]["kind"] == "symlink" and names["inner"]["inside"] is True
    assert SECRET not in repr(listing)


async def test_a_symlink_that_stays_inside_the_tree_is_followed(tools: RepoTools) -> None:
    read = await tools.read(SESSION, "inner/app.py")
    assert read["path"] == "src/app.py"
    assert "1\tdef hello():" in read["text"]
    assert [e["name"] for e in (await tools.ls(SESSION, "inner"))["entries"]] == ["app.py"]


def test_safe_path_resolves_inside_the_root(tree: Tree) -> None:
    assert safe_path(tree.path, "src/app.py") == tree.path / "src" / "app.py"
    assert safe_path(tree.path, "./inner/app.py") == tree.path / "src" / "app.py"
    assert safe_path(tree.path, ".") == tree.path
    with pytest.raises(UnsafePathError, match="outside the worktree"):
        safe_path(tree.path, "secret-link")


@pytest.mark.parametrize(
    ("path", "expected"),
    [("src/app.py", "src/app.py"), ("./src//app.py", "src/app.py"), (".", ".")],
)
def test_normalize_relative_path_accepts_plain_relative_paths(path: str, expected: str) -> None:
    assert normalize_relative_path(path) == expected


@pytest.mark.parametrize(
    "path", ["", "/abs", "a/../b", "..", ".git/config", "a/.Git", "a\0b", "a\nb", "x" * 1025]
)
def test_normalize_relative_path_refuses_unsafe_paths(path: str) -> None:
    with pytest.raises(UnsafePathError):
        normalize_relative_path(path)


# --- sessions ------------------------------------------------------------------------------


async def test_repo_tools_require_a_review_session(tools: RepoTools) -> None:
    for session in (None, "not-a-review-session"):
        with pytest.raises(NoReviewSessionError):
            await tools.read(session, "src/app.py")
        with pytest.raises(NoReviewSessionError):
            await tools.grep(session, "needle")
        with pytest.raises(NoReviewSessionError):
            await tools.ls(session, ".")
        with pytest.raises(NoReviewSessionError):
            await tools.git(session, "log", [])


async def test_mcp_tools_are_read_only_and_need_a_session(
    tree: Tree, registry: ReviewSessionRegistry
) -> None:
    class Host:
        repo_tools = RepoTools(registry.worktree_for, tree.workspace, registry.allowance)

    defs = {tool.name: tool for tool in build_tools(Host())}
    assert set(defs) == {"repo_read", "repo_grep", "repo_ls", "repo_git"}
    assert not any(tool.writes for tool in defs.values())

    async def call(name: str, args: BaseModel, session: str | None = SESSION) -> Any:
        tool: ToolDef = defs[name]
        return await tool.handler(args, CallContext(session))

    with pytest.raises(NoReviewSessionError):
        await call("repo_read", RepoReadIn(path="src/app.py"), session=None)
    read = await call("repo_read", RepoReadIn(path="src/app.py", start=2, end=2))
    assert read["text"] == "2\t    return 'needle v2'\n"
    grep = await call("repo_grep", RepoGrepIn(pattern="needle v2", glob="*.py"))
    assert [m["path"] for m in grep["matches"]] == ["src/app.py"]
    ls = await call("repo_ls", RepoLsIn())
    assert "src" in [e["name"] for e in ls["entries"]]
    log = await call("repo_git", RepoGitIn(cmd="log", args=["--oneline"]))
    assert "second" in log["output"] and "first" in log["output"]


def test_registry_binds_and_unbinds_sessions(tmp_path: Path) -> None:
    sessions = ReviewSessionRegistry()
    assert sessions.worktree_for("s") is None
    sessions.bind("s", tmp_path)
    assert sessions.worktree_for("s") == tmp_path
    assert sessions.allowance.take("s", "a.py", 10) == 10
    sessions.bind("s", tmp_path)  # same tree: the allowance carries on
    assert sessions.allowance.remaining("s", "a.py") == FILE_LINES_PER_SESSION - 10
    sessions.bind("s", tmp_path / "other")  # a different tree starts afresh
    assert sessions.allowance.remaining("s", "a.py") == FILE_LINES_PER_SESSION
    sessions.allowance.take("s", "a.py", 10)
    sessions.unbind("s")
    assert sessions.worktree_for("s") is None
    assert sessions.allowance.remaining("s", "a.py") == FILE_LINES_PER_SESSION
    sessions.unbind("s")  # idempotent
    with pytest.raises(NoReviewSessionError):
        sessions.bind("", tmp_path)


def test_file_line_allowance_is_per_session_and_path() -> None:
    allowance = FileLineAllowance(5)
    assert allowance.take("s1", "a", 3) == 3
    assert allowance.take("s1", "a", 3) == 2
    assert allowance.take("s1", "a", 3) == 0
    assert allowance.remaining("s1", "a") == 0
    assert allowance.remaining("s1", "b") == 5 and allowance.remaining("s2", "a") == 5
    assert allowance.take("s1", "b", 0) == 0
    allowance.drop("s1")
    allowance.drop("never-seen")
    assert allowance.remaining("s1", "a") == 5
    assert allowance.take("s1", "a", -1) == 0 and allowance.remaining("s1", "a") == 5
    allowance.take("s1", "a", 4)
    allowance.refund("s1", "a", 10)  # never refunds more than was used
    assert allowance.remaining("s1", "a") == 5
    allowance.refund("s1", "never-read", 3)
    allowance.refund("nobody", "a", 3)
    assert allowance.remaining("s1", "never-read") == 5


# --- repo_read -------------------------------------------------------------------------------


async def test_repeated_reads_hit_the_per_file_allowance(tools: RepoTools) -> None:
    first = await tools.read(SESSION, "big.txt")
    assert (first["start"], first["end"]) == (1, READ_LINES_PER_CALL)
    assert first["allowance_remaining"] == FILE_LINES_PER_SESSION - READ_LINES_PER_CALL
    second = await tools.read(SESSION, "big.txt", start=1, end=5000)
    assert (second["start"], second["end"]) == (1, READ_LINES_PER_CALL)  # re-reads count too
    assert second["allowance_remaining"] == 0
    with pytest.raises(AllowanceExhaustedError, match="4000 lines per file"):
        await tools.read(SESSION, "big.txt", start=4001)
    # Another file, and the same file in another session, have their own allowance.
    assert (await tools.read(SESSION, "src/app.py"))["end"] == 2


async def test_a_read_is_trimmed_to_what_remains_of_the_allowance(tools: RepoTools) -> None:
    await tools.read(SESSION, "big.txt", start=1, end=2000)
    await tools.read(SESSION, "big.txt", start=2001, end=3990)
    last = await tools.read(SESSION, "big.txt", start=4000, end=4100)
    assert (last["start"], last["end"]) == (4000, 4009)
    assert last["truncated"] is True and "allowance" in last["cap"]


async def test_auto_skipped_files_are_refused(tools: RepoTools) -> None:
    reasons = {
        "pnpm-lock.yaml": "lockfile",
        "vendor/lib.py": "vendored",
        "dist/bundle.js": "generated",
        "image.png": "binary",
    }
    for path, reason in reasons.items():
        with pytest.raises(AutoSkippedFileError, match=reason):
            await tools.read(SESSION, path)
    # Search leaves them out entirely.
    found = await tools.grep(SESSION, "needle")
    paths = {match["path"] for match in found["matches"]}
    assert not paths & set(reasons)
    assert found["skipped_files"] == ["dist/bundle.js", "pnpm-lock.yaml", "vendor/lib.py"]
    for args in (["HEAD:pnpm-lock.yaml"], ["HEAD:image.png"]):
        with pytest.raises(AutoSkippedFileError):
            await tools.git(SESSION, "show", args)
    with pytest.raises(AutoSkippedFileError):
        await tools.git(SESSION, "blame", ["vendor/lib.py"])
    listing = await tools.ls(SESSION, ".")
    skip = {e["name"]: e.get("skip") for e in listing["entries"]}
    assert skip["pnpm-lock.yaml"] == "lockfile" and skip["vendor"] == "vendored"
    assert skip["big.txt"] is None


async def test_read_validates_its_range_and_target(tools: RepoTools) -> None:
    with pytest.raises(RepoToolError, match="start"):
        await tools.read(SESSION, "src/app.py", start=0)
    with pytest.raises(RepoToolError, match="end"):
        await tools.read(SESSION, "src/app.py", start=5, end=4)
    with pytest.raises(RepoPathNotFoundError):
        await tools.read(SESSION, "missing.py")
    with pytest.raises(RepoPathNotFoundError):
        await tools.read(SESSION, "src")
    with pytest.raises(RepoPathNotFoundError):
        await tools.ls(SESSION, "src/app.py")
    past = await tools.read(SESSION, "src/app.py", start=10)
    assert past["text"] == "" and past["end"] == 9 and past["has_more"] is False


async def test_read_stops_at_the_byte_cap(tools: RepoTools, tree: Tree) -> None:
    huge = tree.path / "huge.txt"  # untracked, local to this test
    wide = tree.path / "wide.txt"
    huge.write_text(("y" * 10_000 + "\n") * 100)
    wide.write_text("z" * 300_000)
    try:
        read = await tools.read(SESSION, "huge.txt")
        assert read["end"] < 100 and read["truncated"] is True and "bytes" in read["cap"]
        one = await tools.read(SESSION, "wide.txt")
        assert one["end"] == 1 and one["truncated"] is True
        assert len(one["text"]) == READ_MAX_BYTES + 2  # "1\t" prefix
        wide.write_text("z" * 600_000 + "\nsecond\n")  # the over-long line is skipped
        after = await tools.read(SESSION, "wide.txt", start=2)
        assert after["text"] == "2\tsecond\n"
    finally:
        huge.unlink()
        wide.unlink()


async def test_caps_are_reported(tools: RepoTools) -> None:
    read = await tools.read(SESSION, "big.txt", start=4990)
    assert "2000 lines per call" in read["cap"] and "4000 lines per file" in read["cap"]
    assert read["has_more"] is False and read["end"] == 5000

    found = await tools.grep(SESSION, "needle")
    assert len(found["matches"]) == GREP_MAX_MATCHES
    assert found["truncated"] is True
    assert "200 matches" in found["cap"] and "64 KB" in found["cap"]
    long = await tools.grep(SESSION, "needle x+", glob="long.txt")
    (match,) = long["matches"]
    assert len(match["text"]) == GREP_MAX_LINE_CHARS and match["line_truncated"] is True

    log = await tools.git(SESSION, "log", ["-p"])
    assert "64 KB" in log["cap"]


async def test_grep_matches_count_against_the_file_allowance(
    tools: RepoTools, registry: ReviewSessionRegistry
) -> None:
    for _ in range(13):  # 13 x 300 lines: 100 of the 4000 remain
        await tools.read(SESSION, "many.txt")
    found = await tools.grep(SESSION, "needle", glob="many.txt")
    assert len(found["matches"]) == 100
    assert found["withheld_by_allowance"] == 200
    again = await tools.grep(SESSION, "needle", glob="many.txt")
    assert again["matches"] == [] and again["withheld_by_allowance"] == 300


async def test_grep_reports_a_raw_output_cut(
    tools: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jesinia.apps.review.application.repo_tools.GREP_RAW_BYTES", 100)
    found = await tools.grep(SESSION, "needle", glob="many.txt")
    assert found["truncated"] is True and 0 < len(found["matches"]) < 10


async def test_grep_reads_tracked_content_with_an_extended_regex(tools: RepoTools) -> None:
    found = await tools.grep(SESSION, "needle (in|v2)")
    assert {(m["path"], m["line"]) for m in found["matches"]} == {
        ("src/app.py", 2),
        ("docs/a b:c.md", 2),
    }
    assert (await tools.grep(SESSION, "no such text anywhere"))["matches"] == []
    nested = await tools.grep(SESSION, "needle", glob="docs/*.md")
    assert [m["path"] for m in nested["matches"]] == ["docs/a b:c.md"]


@pytest.mark.parametrize("glob", ["/etc/*", "../*", "a/../b", ":(top)x", "\0", ".git/*"])
async def test_grep_refuses_unsafe_globs(tools: RepoTools, glob: str) -> None:
    with pytest.raises(InvalidSearchError):
        await tools.grep(SESSION, "needle", glob=glob)


async def test_grep_refuses_oversized_or_invalid_patterns(tools: RepoTools) -> None:
    with pytest.raises(InvalidSearchError, match="200"):
        await tools.grep(SESSION, "a" * 201)
    for pattern in ("", "a\0b", "a\nb"):
        with pytest.raises(InvalidSearchError):
            await tools.grep(SESSION, pattern)
    with pytest.raises(RepoToolError):
        await tools.grep(SESSION, "(unclosed")  # git rejects it; the cause is kept


def test_check_glob_matches_bare_names_at_any_depth() -> None:
    assert check_glob("*.py") == ":(glob)**/*.py"
    assert check_glob("src/*.py") == ":(glob)src/*.py"


# --- repo_ls ---------------------------------------------------------------------------------


async def test_ls_lists_directories_with_kinds_and_sizes(tools: RepoTools) -> None:
    listing = await tools.ls(SESSION, "src")
    assert listing["path"] == "src"
    size = len("def hello():\n    return 'needle v2'\n")
    assert listing["entries"] == [{"name": "app.py", "kind": "file", "size": size, "skip": None}]
    assert listing["truncated"] is False


async def test_ls_caps_its_entries(
    tools: RepoTools, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jesinia.apps.review.application.repo_tools.LS_MAX_ENTRIES", 3)
    listing = await tools.ls(SESSION, ".")
    assert len(listing["entries"]) == 3 and listing["truncated"] is True
    assert "3 entries" in listing["cap"]


# --- repo_git --------------------------------------------------------------------------------


async def test_repo_git_refuses_options_outside_the_allowlist(tools: RepoTools) -> None:
    refused = {
        "log": [
            ["--output=x"],
            ["--output", "x"],
            ["-c", "core.pager=sh"],
            ["--exec=sh"],
            ["--ext-diff"],
            ["--textconv"],
            ["--paginate"],
            ["--config-env=a=b"],
            ["--show-signature"],
            ["-L", "1,2:app.py"],
        ],
        "show": [["--output=x"], ["--ext-diff"], ["--textconv"], ["-n", "1"]],
        "blame": [["--contents", "/etc/passwd", "app.py"], ["-S", "revs", "a"], ["--stat"]],
    }
    for cmd, cases in refused.items():
        for args in cases:
            with pytest.raises(UnsafeGitArgumentError):
                await tools.git(SESSION, cmd, args)
    with pytest.raises(UnsafeGitArgumentError, match="log, show or blame"):
        await tools.git(SESSION, "diff", [])


async def test_repo_git_rejects_unsafe_args(tools: RepoTools) -> None:
    unsafe = [
        ("log", ["-n", "0"]),
        ("log", ["-n", "-1"]),
        ("log", ["-n", "100000"]),
        ("log", ["-n"]),
        ("log", ["-nfoo"]),
        ("log", ["HEAD..main"]),
        ("log", ["-HEAD"]),
        ("log", ["HEAD\0"]),
        ("log", ["HEAD\x1b[31m"]),
        ("log", ["main:src/app.py"]),
        ("log", ["--since="]),
        ("log", ["--author=" + "a" * 101]),
        ("log", ["--", "../outside"]),
        ("log", ["--", "/etc/passwd"]),
        ("log", ["a", "b", "c", "d", "e", "f", "g", "h", "i"]),
        ("log", ["--oneline"] * 33),
        ("log", ["a" * 301]),
        ("show", ["HEAD:"]),
        ("show", [":src/app.py"]),
        ("show", ["HEAD:../../x"]),
        ("show", ["HEAD:/etc/passwd"]),
        ("show", ["HEAD:src/app.py", "HEAD"]),
        ("blame", ["-L", "1,2:hello"]),
        ("blame", ["-L", ":hello"]),
        ("blame", ["-L", "5,2", "src/app.py"]),
        ("blame", ["-L", "0,2", "src/app.py"]),
        ("blame", ["-L"]),
        ("blame", []),
        ("blame", ["HEAD", "HEAD", "src/app.py"]),
        ("blame", ["--", "a.py", "b.py"]),
        ("blame", ["../x"]),
    ]
    for cmd, args in unsafe:
        with pytest.raises((UnsafeGitArgumentError, UnsafePathError)):
            plan_git_command(cmd, args, dialect=BSD_REGEX)


@pytest.mark.parametrize(
    "revision", ["HEAD", "HEAD~3", "HEAD^2", "main", "refs/heads/main", "a" * 40, "v1.2.3"]
)
def test_check_revision_accepts_ordinary_revisions(revision: str) -> None:
    assert check_revision(revision) == revision


async def test_repo_git_runs_allowlisted_log_show_and_blame(tools: RepoTools, tree: Tree) -> None:
    log = await tools.git(
        SESSION,
        "log",
        ["--oneline", "-n", "1", "--stat", "--since=2000-01-01", "--author=Test", "HEAD"],
    )
    assert "second" in log["output"] and "first" not in log["output"]
    follow = await tools.git(SESSION, "log", ["--follow", "--oneline", "--", "src/app.py"])
    assert "first" in follow["output"]
    show = await tools.git(SESSION, "show", [f"{tree.first}:src/app.py"])
    assert show["output"] == "def hello():\n    return 'needle'\n"
    stat = await tools.git(SESSION, "show", ["--stat", "--oneline", tree.first])
    assert "src/app.py" in stat["output"]
    blame = await tools.git(SESSION, "blame", ["-L", "2,2", tree.head, "src/app.py"])
    assert "needle v2" in blame["output"] and blame["output"].count("\n") == 1
    joined = await tools.git(SESSION, "blame", ["-L1,1", "--", "src/app.py"])
    assert "def hello" in joined["output"]


async def test_repo_git_output_counts_against_the_file_allowance(
    tools: RepoTools, registry: ReviewSessionRegistry
) -> None:
    await tools.read(SESSION, "big.txt", start=1, end=2000)
    await tools.read(SESSION, "big.txt", start=1, end=1995)
    shown = await tools.git(SESSION, "show", ["HEAD:big.txt"])
    assert shown["output"].count("\n") == 5 and shown["truncated"] is True
    with pytest.raises(AllowanceExhaustedError):
        await tools.git(SESSION, "blame", ["big.txt"])


async def test_repo_git_output_is_capped(tools: RepoTools, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jesinia.apps.review.application.repo_tools.GIT_MAX_BYTES", 200)
    capped = await tools.git(SESSION, "log", ["-p"])
    assert capped["truncated"] is True and len(capped["output"].encode()) <= 200
    monkeypatch.setattr("jesinia.apps.review.application.repo_tools.GIT_MAX_LINES", 3)
    lines = await tools.git(SESSION, "log", [])
    assert lines["output"].count("\n") == 3 and lines["truncated"] is True


async def test_repo_git_failures_keep_gits_explanation(tools: RepoTools) -> None:
    with pytest.raises(RepoGitError) as caught:
        await tools.git(SESSION, "log", ["--follow", "--", "a.py", "b.py"])
    assert caught.value.__cause__ is not None
    assert "follow" in str(caught.value)


async def test_repo_git_runs_hardened(
    tools: RepoTools, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[tuple[str, ...], dict[str, str]]] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def recording_spawn(*argv: str, **kwargs: Any) -> Any:
        seen.append((argv, kwargs["env"]))
        return await real_spawn(*argv, **kwargs)

    await tree.workspace.regex_dialect()  # probed once per workspace; covered on its own
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    monkeypatch.setattr(SPAWN, recording_spawn)
    await tools.git(SESSION, "log", ["-p"])
    await tools.git(SESSION, "show", [])
    await tools.git(SESSION, "blame", ["src/app.py"])
    await tools.grep(SESSION, "needle", glob="*.py")
    await tools.grep(SESSION, "needle")
    by_cmd: dict[str, list[tuple[tuple[str, ...], dict[str, str]]]] = {}
    for argv, env in seen:
        command = next(
            a for a in argv if a in {"log", "show", "blame", "grep", "rev-parse", "merge-base"}
        )
        by_cmd.setdefault(command, []).append((argv, env))
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "--no-pager" in argv and "core.hooksPath=/dev/null" in argv
        assert "log.showSignature=false" in argv and "color.ui=never" in argv
        assert f"--attr-source={EMPTY_TREE}" in argv  # the PR's .gitattributes are ignored
        assert "blame.ignoreRevsFile=" in argv and "log.mailmap=false" in argv
        assert "mailmap.file=" in argv and "mailmap.blob=" in argv
        assert "grep.threads=2" in argv
    assert sorted(len(runs) for runs in by_cmd.values()) == [1, 1, 1, 1, 1, 2]
    for command in ("log", "show", "blame"):
        ((argv, _),) = by_cmd[command]
        assert "--no-textconv" in argv and "--no-ext-diff" in argv
        assert "--end-of-options" in argv
    for command in ("log", "show", "grep", "rev-parse", "merge-base"):
        assert all(argv[argv.index("-C") + 1] == str(tree.path) for argv, _ in by_cmd[command])
    ((log, _),) = by_cmd["log"]
    assert "grep.patternType=extended" in log
    (glob_grep, glob_env), (plain_grep, plain_env) = by_cmd["grep"]
    assert "--cached" in glob_grep and "--no-column" in glob_grep
    assert "-F" in glob_grep and "-E" not in glob_grep  # a plain word is searched literally
    assert glob_env["GIT_LITERAL_PATHSPECS"] == "0"  # only for the :(glob) pathspec
    assert plain_env["GIT_LITERAL_PATHSPECS"] == "1"
    # blame runs on the mirror, with no worktree whose .mailmap it could read, at the
    # worktree's HEAD resolved to a full id.
    ((blame, _),) = by_cmd["blame"]
    assert "-C" not in blame
    mirror = tree.path.parent.parent / "mirror.git"
    assert blame[blame.index("--git-dir") + 1] == str(mirror.resolve())
    assert blame[blame.index("--end-of-options") + 1 :] == (tree.head, "src/app.py")


async def test_tree_reads_refuse_trees_outside_the_workspace(tree: Tree, tmp_path: Path) -> None:
    git_port: TreeGit = tree.workspace  # checked by mypy
    with pytest.raises(RepoToolError):
        await RepoTools(lambda _: tmp_path, git_port, FileLineAllowance(10)).git(SESSION, "log", [])


async def test_a_vanished_worktree_is_reported(tree: Tree, tmp_path: Path) -> None:
    gone = RepoTools(lambda _: tmp_path / "gone", tree.workspace, FileLineAllowance(10))
    with pytest.raises(NoReviewSessionError, match="no longer exists"):
        await gone.ls(SESSION, ".")


# --- wiring ----------------------------------------------------------------------------------


async def test_review_module_registers_the_repo_tools(cfg: Config) -> None:
    module = ReviewModule()
    names = [tool.name for tool in module.mcp_tools()]
    assert names == ["repo_read", "repo_grep", "repo_ls", "repo_git"]
    tool = module.mcp_tools()[0]
    with pytest.raises(Exception, match="not started"):
        await tool.handler(RepoReadIn(path="a"), CallContext(SESSION))
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    assert module.sessions.worktree_for(SESSION) is None
    with pytest.raises(NoReviewSessionError):
        await tool.handler(RepoReadIn(path="a"), CallContext(SESSION))
    await module.stop()


# --- fix round 1: bypasses found in review ------------------------------------------------


async def test_concurrent_reads_never_exceed_the_allowance(tools: RepoTools) -> None:
    reads = await asyncio.gather(
        *(tools.read(SESSION, "big.txt", start=1, end=2000) for _ in range(10)),
        return_exceptions=True,
    )
    returned = 0
    for r in reads:
        if isinstance(r, BaseException):
            assert isinstance(r, AllowanceExhaustedError)
        else:
            returned += r["end"] - r["start"] + 1
    assert returned == FILE_LINES_PER_SESSION


async def test_concurrent_git_reads_never_exceed_the_allowance(tools: RepoTools) -> None:
    shows = await asyncio.gather(
        *(tools.git(SESSION, "show", ["HEAD:big.txt"]) for _ in range(5)),
        return_exceptions=True,
    )
    returned = sum(r["output"].count("\n") for r in shows if not isinstance(r, BaseException))
    assert 0 < returned <= FILE_LINES_PER_SESSION


async def test_spelling_aliases_share_the_allowance(tools: RepoTools, tree: Tree) -> None:
    await tools.read(SESSION, "big.txt", start=1, end=2000)
    await tools.read(SESSION, "alias.txt", start=1, end=1000)  # a symlink to big.txt
    third = await tools.read(SESSION, "./big.txt", start=1, end=500)
    assert third["allowance_remaining"] == FILE_LINES_PER_SESSION - 3500
    if (tree.path / "BIG.TXT").exists():  # case-insensitive filesystem (APFS default)
        upper = await tools.read(SESSION, "BIG.TXT", start=1, end=10)
        assert upper["allowance_remaining"] == FILE_LINES_PER_SESSION - 3510
    nfc, nfd = "caf\u00e9.txt", "cafe\u0301.txt"
    first = await tools.read(SESSION, nfc)
    assert first["allowance_remaining"] == FILE_LINES_PER_SESSION - 1
    if (tree.path / nfd).exists():  # normalization-insensitive filesystem
        second = await tools.read(SESSION, nfd)
        assert second["allowance_remaining"] == FILE_LINES_PER_SESSION - 2


async def test_show_refuses_blobs_and_trees(tools: RepoTools, tree: Tree) -> None:
    blob = git(tree.path, "rev-parse", "HEAD:pnpm-lock.yaml")
    for revision in (blob, blob[:10], "HEAD^{tree}", git(tree.path, "rev-parse", "HEAD^{tree}")):
        with pytest.raises(NotACommitError, match="repo_read"):
            await tools.git(SESSION, "show", [revision])
    with pytest.raises(NotACommitError):
        await tools.git(SESSION, "show", [f"{blob}:x"])
    with pytest.raises(NotACommitError):
        await tools.git(SESSION, "log", [blob])
    with pytest.raises(NotACommitError):
        await tools.git(SESSION, "blame", [blob, "src/app.py"])
    commit = await tools.git(SESSION, "show", ["--stat", "HEAD~1"])
    assert "first" in commit["output"]


async def test_show_of_a_deleted_file_counts_against_its_allowance(
    tools: RepoTools, tree: Tree
) -> None:
    shown = await tools.git(SESSION, "show", [f"{tree.first}:old.txt"])
    assert shown["output"] == "old content\n"
    assert f"({FILE_LINES_PER_SESSION - 1} lines remain)" in shown["cap"]


async def test_git_paths_are_passed_as_written_not_as_resolved(tools: RepoTools) -> None:
    # inner is a symlink in the tree: git's <rev>:<path> doesn't follow it.
    with pytest.raises(RepoGitError):
        await tools.git(SESSION, "show", ["HEAD:inner/app.py"])
    assert (await tools.git(SESSION, "show", ["HEAD:src/app.py"]))["output"].startswith("def")


async def test_pr_gitattributes_cannot_hide_files(tools: RepoTools) -> None:
    found = await tools.grep(SESSION, "secretive_marker")
    assert {m["path"] for m in found["matches"]} == {"hidden.py", "hidden2.py"}
    log = await tools.git(SESSION, "log", ["-p", "-n", "1", "--", "hidden.py"])
    assert "+secretive_marker = 1" in log["output"]
    show = await tools.git(SESSION, "show", ["-p", "--", "hidden2.py"])
    assert "+secretive_marker = 2" in show["output"]


async def test_a_newline_in_a_filename_is_handled(tools: RepoTools) -> None:
    found = await tools.grep(SESSION, "newline_marker")
    assert found["matches"] == []
    assert found["skipped_files"] == ["evil\nsrc/x.py"]
    # Ordinary matches before and after it are unaffected.
    both = await tools.grep(SESSION, "(newline|secretive)_marker")
    assert {m["path"] for m in both["matches"]} == {"hidden.py", "hidden2.py"}


async def test_garbled_grep_output_is_a_typed_error(
    tools: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def garbled(*_: Any, **__: Any) -> CappedOutput:
        return CappedOutput(b"a.py\0notanumber\0text\n", False)

    monkeypatch.setattr(tools, "_run_git", garbled)
    with pytest.raises(RepoGitError, match="could not parse"):
        await tools.grep(SESSION, "text")


# The re-review's bypasses: quantified alternation (with an empty branch) cost 5.2 GB in git.
GREP_BLOWUP = "(" + "|".join("abcdefghijklmnopqrstuvwxyz0123456789ABCD") + "|()){50}"
AUTHOR_BLOWUP = "(" + "|".join("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN") + "|()){50}"


@pytest.mark.parametrize(
    "pattern",
    [
        GREP_BLOWUP,
        AUTHOR_BLOWUP,
        "(.|()){50}",
        "(ab)+",
        "(a|b){3}",
        "(a|b)+",
        "()",
        "(|a)",
        "a|",
        "a{1,100}",
        "((a{1,255}){1,255}){1,40}b",
        "x(a{1,255}){1,255}",
        "a{51}",
        "a{,5}",
        "a{5,}",
        "a{5,2}",
        "(a+)+",
        "a+*",
        "a{2}{3}",
        "*a",
        "^*",
        "\\b+",
        "(a)\\1",
        "\\q",
        "\\",
        "x{",
        "{}",
        "[abc",
        "[[:alpha]+",
        "(unclosed",
        "closed)",
        "|".join("abcdefghijklmnopqrstu"),  # 21 alternatives
        "x" * 201,
        ".*" * 99 + "Q",  # the re-review's CPU blowups: chained open-ended repeats
        ".*" * 49 + "Q",
        "a*b+c*d+e*",  # a 5th unbounded repeat
        "x{1,50}y{1,50}z{1}",  # finite bounds summing to 101
        "x{50}y{50}z?{1}",
    ],
)
@pytest.mark.parametrize("dialect", [BSD_REGEX, GNU_REGEX])
def test_patterns_outside_the_allowed_regex_subset_are_refused(
    pattern: str, dialect: RegexDialect
) -> None:
    started = time.perf_counter()
    assert regex_problem(pattern, dialect) is not None
    with pytest.raises(InvalidSearchError):
        check_pattern(pattern, dialect)
    assert time.perf_counter() - started < 0.1


@pytest.mark.parametrize(
    "pattern",
    [
        "foo\\(",
        "def \\w+",
        "a|b",
        "(foo|bar)baz",
        "x{2,5}",
        "[A-Z]+_id",
        "a{0}",
        "a{1,50}",
        "^start.*end$",
        "\\bword\\b",
        "\\W\\S\\D\\.\\{\\}",
        "[(+]+",
        "[^]a]+",
        "[]a]*",
        "[[:alpha:]]+",
        "a]b}c",
        "((a|b)c|d)e",
        "|".join("abcdefghijklmnopqrst"),  # 20 alternatives
        ".*foo.*bar",
        "\\w+_id",
        "a.*b.*c.*d",  # 3 unbounded repeats
        "a*b+c*d+e?f?",  # 4 unbounded; ? is bounded
        "x{1,50}y{1,50}",  # bounds summing to 100
    ],
)
@pytest.mark.parametrize("dialect", [BSD_REGEX, GNU_REGEX])
def test_patterns_in_the_allowed_regex_subset_pass(pattern: str, dialect: RegexDialect) -> None:
    assert regex_problem(pattern, dialect) is None


def test_not_a_word_boundary_is_allowed_only_where_git_supports_it() -> None:
    assert regex_problem("f\\Boo", GNU_REGEX) is None
    assert regex_problem("f\\Boo", BSD_REGEX) is not None
    assert regex_problem("\\B+", GNU_REGEX) is not None  # an anchor; never repeated


async def test_ordinary_patterns_still_search(tools: RepoTools) -> None:
    for pattern, path in (
        ("def \\w+", "src/app.py"),
        ("hello\\(", "src/app.py"),
        ("\\bhello\\b", "src/app.py"),
        ("(def|class) hel", "src/app.py"),
    ):
        found = await tools.grep(SESSION, pattern)
        assert path in {m["path"] for m in found["matches"]}, pattern
    either = await tools.grep(SESSION, "secretive_marker = 1|old content")
    assert [m["path"] for m in either["matches"]] == ["hidden.py"]
    with pytest.raises(InvalidSearchError):
        await tools.grep(SESSION, "(a+)+$")


async def test_author_patterns_get_the_same_regex_check(tools: RepoTools) -> None:
    for author in ("(a{1,50}){1,50}", AUTHOR_BLOWUP):
        with pytest.raises(UnsafeGitArgumentError, match="--author"):
            await tools.git(SESSION, "log", [f"--author={author}"])
    found = await tools.git(SESSION, "log", ["--oneline", "--author=Te(s|x)t"])
    assert "second" in found["output"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("def \\w+", "def [[:alnum:]_]+"),
        ("\\s\\S\\d\\D\\W", "[[:space:]][^[:space:]][[:digit:]][^[:digit:]][^[:alnum:]_]"),
        ("[\\w]\\(", "[\\w]\\("),
        ("end\\", "end\\"),
        ("\\bword\\b", "[[:<:]]word[[:>:]]"),
        ("\\b\\w+ \\b(x)", "[[:<:]][[:alnum:]_]+ [[:<:]](x)"),
        ("\\w+\\b", "[[:alnum:]_]+[[:>:]]"),
    ],
)
def test_posix_ere_spells_out_shorthand_classes(pattern: str, expected: str) -> None:
    assert posix_ere(pattern, BSD_REGEX) == expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("def \\w+", "def [[:alnum:]_]+"),
        ("\\bword\\b", "\\bword\\b"),
        ("f\\Boo\\s", "f\\Boo[[:space:]]"),
    ],
)
def test_posix_ere_keeps_native_word_boundaries(pattern: str, expected: str) -> None:
    assert posix_ere(pattern, GNU_REGEX) == expected


@pytest.mark.parametrize("dialect", [BSD_REGEX, GNU_REGEX])
async def test_grep_and_author_use_the_dialect_git_has(
    tree: Tree,
    registry: ReviewSessionRegistry,
    monkeypatch: pytest.MonkeyPatch,
    dialect: RegexDialect,
) -> None:
    async def injected() -> RegexDialect:
        return dialect

    monkeypatch.setattr(tree.workspace, "regex_dialect", injected)
    seen: list[tuple[str, ...]] = []
    real_spawn: Callable[..., Any] = asyncio.create_subprocess_exec

    async def recording_spawn(*argv: str, **kwargs: Any) -> Any:
        # Record only: the other dialect's syntax would be invalid for the real git here.
        seen.append(argv)
        return await real_spawn("true", **kwargs)

    monkeypatch.setattr(SPAWN, recording_spawn)
    tools = RepoTools(registry.worktree_for, tree.workspace, registry.allowance)
    await tools.grep(SESSION, "\\bhello\\b")
    await tools.git(SESSION, "log", ["--author=\\bTest"])
    start, end = ("\\b", "\\b") if dialect.native_word_boundaries else ("[[:<:]]", "[[:>:]]")
    assert seen[0][seen[0].index("-e") + 1] == f"{start}hello{end}"
    assert f"--author={start}Test" in seen[1]


async def test_real_git_searches_word_boundaries_in_its_own_dialect(tools: RepoTools) -> None:
    # Runs against whatever git is installed (BSD regex on macOS, glibc on Linux CI).
    found = await tools.grep(SESSION, "\\bhello\\b")
    assert [m["path"] for m in found["matches"]] == ["src/app.py"]
    assert (await tools.grep(SESSION, "\\bello\\b"))["matches"] == []
    log = await tools.git(SESSION, "log", ["--oneline", "--author=\\bTest\\b"])
    assert "second" in log["output"]


def test_grep_output_cut_mid_record_drops_that_record() -> None:
    cut = b"a.py\x001\x00one\nb\nc.py\x002\x00two\nd.py\x003\x00thr"
    assert _parse_grep(cut) == ([("a.py", 1, b"one"), ("b\nc.py", 2, b"two")], True)
    assert _parse_grep(b"") == ([], False)
    assert _parse_grep(b"a.py\x001\x00one\n") == ([("a.py", 1, b"one")], False)
    # A NUL inside matched text doesn't misalign the records that follow it.
    nul = b"a.py\x001\x00x\x00y\x00z\nb.py\x002\x00ok\n"
    assert _parse_grep(nul) == ([("a.py", 1, b"x\x00y\x00z"), ("b.py", 2, b"ok")], False)


# --- fix round 2: revisions stay inside the PR's history -----------------------------------


async def test_revisions_outside_the_pr_history_are_refused(tools: RepoTools, tree: Tree) -> None:
    other_worktree = _worktree_ref(tree)
    outside = ["main", "other", tree.other, tree.other[:12], other_worktree]
    for revision in outside:
        for cmd, args in (
            ("log", [revision]),
            ("show", [revision]),
            ("show", [f"{revision}:other.txt"]),
            ("blame", [revision, "src/app.py"]),
        ):
            with pytest.raises(OutsidePrHistoryError, match="not part of this PR's history"):
                await tools.git(SESSION, cmd, args)


async def test_revisions_in_the_pr_history_are_allowed(tools: RepoTools, tree: Tree) -> None:
    merge_base = git(tree.path, "merge-base", "HEAD", tree.other)
    assert merge_base == tree.first
    for revision in ("HEAD", "HEAD~1", merge_base, tree.head):
        shown = await tools.git(SESSION, "show", ["--oneline", "--stat", revision])
        assert shown["output"]
    blame = await tools.git(SESSION, "blame", ["HEAD~1", "src/app.py"])
    assert "needle" in blame["output"]


def _worktree_ref(tree: Tree) -> str:
    """``worktrees/<name>/HEAD`` naming the other PR's worktree, as seen from this one."""
    admin = Path(git(tree.path, "rev-parse", "--git-common-dir")) / "worktrees"
    for name in sorted(p.name for p in admin.iterdir()):
        ref = f"worktrees/{name}/HEAD"
        if git(tree.path, "rev-parse", ref) == tree.other:
            return ref
    raise AssertionError("no worktree ref for the other PR")


async def test_unbind_frees_the_sessions_lock_and_allowance(tree: Tree) -> None:
    sessions = ReviewSessionRegistry()
    sessions.bind(SESSION, tree.path)
    tools = RepoTools(sessions.worktree_for, tree.workspace, sessions.allowance)
    await tools.read(SESSION, "src/app.py")
    assert sessions.allowance.tracks(SESSION)
    sessions.unbind(SESSION)
    assert not sessions.allowance.tracks(SESSION)
    assert not sessions.allowance.tracks("never-bound")


async def test_a_nul_in_a_matched_line_never_hides_later_matches(tools: RepoTools) -> None:
    found = await tools.grep(SESSION, "nul_marker")
    assert [(m["path"], m["line"]) for m in found["matches"]] == [
        ("aaa-nul.txt", 2501),
        ("zzz-normal.txt", 1),
    ]
    assert found["matches"][0]["text"] == "nul_marker\x00tail"
    assert found["truncated"] is False


async def test_blame_ignores_the_prs_mailmap(tools: RepoTools) -> None:
    blame = await tools.git(SESSION, "blame", ["src/app.py"])
    assert "Test" in blame["output"] and "Mallory" not in blame["output"]
    log = await tools.git(SESSION, "log", ["-n", "1"])
    assert "Mallory" not in log["output"]


async def test_a_failed_regex_probe_fails_the_search_with_its_cause(
    tree: Tree, registry: ReviewSessionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken() -> RegexDialect:
        raise GitCommandError(("git", "grep"), 2, "regex library missing")

    monkeypatch.setattr(tree.workspace, "regex_dialect", broken)
    tools = RepoTools(registry.worktree_for, tree.workspace, registry.allowance)
    with pytest.raises(RepoGitError, match="regex library missing") as caught:
        await tools.grep(SESSION, "a|b")
    assert isinstance(caught.value.__cause__, GitCommandError)


# --- fix round 5: CPU budget -----------------------------------------------------------------


class _CountingGit:
    """Wraps the workspace's ``read_in_tree`` to record concurrency and timeouts."""

    def __init__(self, workspace: GitWorkspace) -> None:
        self.workspace = workspace
        self.running: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.timeouts: list[tuple[str, float | None]] = []

    async def read_in_tree(self, tree: Path, args: Sequence[str], **kwargs: Any) -> CappedOutput:
        key = str(tree)
        self.running[key] = self.running.get(key, 0) + 1
        self.peak[key] = max(self.peak.get(key, 0), self.running[key])
        self.timeouts.append((args[0], kwargs.get("timeout")))
        try:
            await asyncio.sleep(0.01)  # widen the window in which overlap would show
            return await self.workspace.read_in_tree(tree, args, **kwargs)
        finally:
            self.running[key] -= 1

    async def regex_dialect(self) -> RegexDialect:
        return await self.workspace.regex_dialect()


async def test_one_session_never_runs_two_gits_at_once(
    tree: Tree, registry: ReviewSessionRegistry
) -> None:
    counting = _CountingGit(tree.workspace)
    tools = RepoTools(
        registry.worktree_for, counting, registry.allowance, git_slots=registry.git_slot
    )
    await asyncio.gather(
        *(tools.grep(SESSION, "needle") for _ in range(3)),
        *(tools.git(SESSION, "log", ["--oneline", "HEAD~1"]) for _ in range(3)),
        tools.git(SESSION, "blame", ["src/app.py"]),
    )
    assert counting.peak == {str(tree.path.resolve()): 1}


async def test_separate_sessions_still_run_in_parallel(tree: Tree, tmp_path: Path) -> None:
    sessions = ReviewSessionRegistry()
    sessions.bind("s1", tree.path)
    sessions.bind("s2", tree.other_tree)
    counting = _CountingGit(tree.workspace)
    tools = RepoTools(
        sessions.worktree_for, counting, sessions.allowance, git_slots=sessions.git_slot
    )
    await asyncio.gather(tools.grep("s1", "needle"), tools.grep("s2", "other PR"))
    assert set(counting.peak.values()) == {1} and len(counting.peak) == 2


async def test_searches_get_a_shorter_time_limit(
    tree: Tree, registry: ReviewSessionRegistry
) -> None:
    counting = _CountingGit(tree.workspace)
    tools = RepoTools(registry.worktree_for, counting, registry.allowance)
    await tools.grep(SESSION, "needle")
    await tools.git(SESSION, "log", ["--oneline", "--author=Test"])
    await tools.git(SESSION, "log", ["--oneline"])
    await tools.git(SESSION, "show", ["--stat", "HEAD~1"])
    assert counting.timeouts == [
        ("grep", SEARCH_TIMEOUT),
        ("log", SEARCH_TIMEOUT),
        ("log", None),
        ("rev-parse", None),
        ("merge-base", None),
        ("show", None),
    ]


def test_unbind_releases_the_sessions_git_slot(tmp_path: Path) -> None:
    sessions = ReviewSessionRegistry()
    sessions.bind("s", tmp_path)
    slot = sessions.git_slot("s")
    assert sessions.git_slot("s") is slot
    sessions.unbind("s")
    assert sessions.git_slot("s") is not slot
