# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental re-reviews (plan Task 12, spec §4.2): the author's previous comments, mapped to
the new commits, and only what changed since walked through.

Fixture repos are real git in tmp dirs (``GIT_CONFIG_GLOBAL=/dev/null``), fetched into the real
``GitWorkspace``; ``review.sqlite`` is real; GitHub and the agent jobs are doubles.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest

from jesinia.apps.review.application.errors import InvalidAgentOutput
from jesinia.apps.review.application.followup import FollowUps
from jesinia.apps.review.application.jobs import parse_job_output, schema_for
from jesinia.apps.review.application.pipeline import Pipeline
from jesinia.apps.review.application.queue import QueueService
from jesinia.apps.review.application.rereview import Rereviews
from jesinia.apps.review.application.scheduler import Scheduler
from jesinia.apps.review.domain.coverage import (
    HunkRef,
    Skip,
    Step,
    coverage,
    hunk_index,
    parse_unified_diff,
)
from jesinia.apps.review.domain.followup import FollowUp
from jesinia.apps.review.domain.jobs import JobKind
from jesinia.apps.review.domain.posting import Draft
from jesinia.apps.review.domain.queue import (
    PrNotInQueueError,
    PrNotOpenError,
    PrRef,
    RepoConfig,
    snapshot_of,
)
from jesinia.apps.review.domain.rereview import (
    InvalidPreviousCommentError,
    PreviousComment,
    PreviousReview,
    RereviewInputError,
    changed_hunks,
    comments_from_posting,
    comments_from_threads,
    comments_listing,
    new_changes_text,
    only_hunks,
    rereview_artifact,
)
from jesinia.apps.review.domain.workspace import DiffTooLargeError, GitCommandError
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.infrastructure.workspace import GitWorkspace
from jesinia.apps.review.module import REVIEW_DB
from jesinia.core.db import open_db
from jesinia.core.events import EventBus
from jesinia.services.github.application.service import GitHubRequestError
from jesinia.services.github.domain.pr import ReviewEvent, ReviewSummary, ReviewThread
from tests.apps.review.fakes import START, FakeClock, FakePullRequests, pull
from tests.apps.review.test_followup import commit, git, text
from tests.apps.review.test_pipeline import FakeFiles, FakeGitHub, FakeJobs, FakeWorkspace

REPO = "acme/web"
LINES = [f"line {i}" for i in range(1, 41)]
LIB = [f"lib {i}" for i in range(1, 21)]
MARKER = "\n\n<!-- jesinia-posting:{} -->"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)


@dataclass(frozen=True)
class Repo:
    """A PR reviewed at ``head`` (``app.py`` lines 5 and 30, ``lib.py`` line 3), then pushed
    to (``pushed``: line 30 changed again, ``lib.py`` renamed to ``pkg_lib.py``), or rebased
    onto ``rebased_base`` with line 30 changed (``rebased``)."""

    remote: Path
    base: str
    head: str
    pushed: str
    rebased_base: str
    rebased: str


def _app(five: str, thirty: str) -> str:
    lines = [*LINES]
    lines[4], lines[29] = five, thirty
    return text(lines)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "main")
    base = commit(work, {"app.py": text(LINES), "lib.py": text(LIB), "notes.txt": "n\n"}, "base")
    git(work, "checkout", "-q", "-b", "feature")
    lib = [*LIB]
    lib[2] = "lib three"
    head = commit(work, {"app.py": _app("line five", "line thirty"), "lib.py": text(lib)}, "pr")
    (work / "lib.py").rename(work / "pkg_lib.py")
    pushed = commit(work, {"app.py": _app("line five", "line thirty, fixed")}, "fix")

    git(work, "checkout", "-q", "-b", "main2", base)
    rebased_base = commit(work, {"notes.txt": "n, changed\n"}, "unrelated")
    git(work, "checkout", "-q", "-b", "rebased", rebased_base)
    rebased = commit(
        work, {"app.py": _app("line five", "line thirty, rebased"), "lib.py": text(lib)}, "pr"
    )
    for branch in ("main", "feature", "main2", "rebased"):
        git(work, "push", "-q", "origin", branch)
    return Repo(remote, base, head, pushed, rebased_base, rebased)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, REVIEW_DB)
    yield connection
    connection.close()


REREVIEW_OUTPUT: dict[str, object] = {
    "requests": [
        {
            "comment_ref": "c2",
            "status": "addressed",
            "explanation": "Line 30 is fixed.",
            "hunks": [{"path": "app.py", "hunk": 1}, {"path": "ghost.py", "hunk": 0}],
        },
        {"comment_ref": "c9", "status": "partly", "explanation": "Unknown.", "hunks": []},
        {"comment_ref": "c2", "status": "not_addressed", "explanation": "Twice.", "hunks": []},
    ],
    "new_changes_walkthrough": {
        "steps": [
            {"title": "The fix", "body": "b", "hunks": [{"path": "app.py", "hunk": 1}]},
            {"title": "Old news", "body": "b", "hunks": [{"path": "app.py", "hunk": 0}]},
        ],
        "skipped": [],
    },
}


@dataclass
class Env:
    store: SqliteReviewStore
    github: FakePullRequests
    workspace: GitWorkspace
    clock: FakeClock
    followups: FollowUps
    rereviews: Rereviews
    jobs: FakeJobs
    pipeline_github: FakeGitHub
    pipeline: Pipeline
    repo: Repo
    ref: PrRef

    def sync(self, head: str, base: str, *, ci: str = "passing") -> None:
        pr = pull(1, REPO, head=head, base=base)
        self.github.inbox = [pr]
        self.pipeline_github.pr = replace(pr, commit_count=1)
        snapshot = snapshot_of(
            pr,
            ci_state=ci,  # type: ignore[arg-type]
            reviews=[],
            automated_reviewer=None,
            viewer="me",
        )
        self.store.apply_sync([snapshot], [], [], self.clock() - timedelta(minutes=20))

    def posted_round(
        self,
        head: str,
        base: str,
        event: ReviewEvent,
        body: str,
        comments: list[dict[str, object]],
    ) -> int:
        """A round on ``head`` whose review (``event``) Jesinia posted, as GitHub lists it."""
        round_id = self.store.start_round(self.ref, head, base, "quick", self.clock())
        self.store.finish_round(round_id, self.clock(), None, "needs_closer_look")
        posting = self.store.start_posting(
            round_id,
            event,
            "k",
            "d",
            body,
            comments,
            self.clock(),
        )
        self.store.finish_posting(posting.id, "https://github.com/acme/web/pull/1#r1")
        state = {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[event]
        self.github.reviews_of[(REPO, 1)] = [
            ReviewSummary(
                "me", False, state, head, START, len(comments), body + MARKER.format(posting.id)
            )
        ]
        return round_id


@pytest.fixture
def env(conn: sqlite3.Connection, repo: Repo, tmp_path: Path) -> Env:
    store = SqliteReviewStore(conn)
    store.upsert_repo(RepoConfig(REPO, watched=True))
    store.set_viewer("me")
    github = FakePullRequests()
    workspace = GitWorkspace(tmp_path / "repos", lambda name: str(repo.remote))
    clock = FakeClock()
    followups = FollowUps(github=github, workspace=workspace, store=store)
    rereviews = Rereviews(github=github, workspace=workspace, store=store)
    jobs = FakeJobs(store)
    jobs.outputs[JobKind.REREVIEW] = REREVIEW_OUTPUT
    pipeline_github = FakeGitHub(replace(pull(1, REPO, head=repo.head, base=repo.base)))
    pipeline = Pipeline(
        github=pipeline_github,
        files=FakeFiles(),
        workspace=workspace,
        jobs=jobs,
        store=store,
        bus=EventBus(),
        clock=clock,
        slots=Scheduler(store, clock),
        followups=followups,
        rereviews=rereviews,
    )
    return Env(
        store,
        github,
        workspace,
        clock,
        followups,
        rereviews,
        jobs,
        pipeline_github,
        pipeline,
        repo,
        PrRef(REPO, 1),
    )


async def stored_diff(env: Env, base: str, head: str) -> str:
    await env.workspace.ensure_mirror(REPO)
    await env.workspace.fetch(REPO, [base, head])
    return await env.workspace.diff(REPO, base, head)


def _rereview_round(env: Env) -> object:
    return next(
        r for k, r in zip(env.jobs.ran, env.jobs.rounds, strict=True) if k is JobKind.REREVIEW
    )


# --- the brief's tests --------------------------------------------------------------------------


def test_rereview_maps_each_request_to_status() -> None:
    diff = parse_unified_diff(
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,2 +10,2 @@\n x\n-y\n+Y\n"
    )
    previous = PreviousReview(
        "round",
        "a" * 40,
        (
            PreviousComment("c1", "A few notes."),
            PreviousComment("c2", "Off by one.", path="app.py", line=10, side="RIGHT"),
            PreviousComment("c3", "Rename it.", path="app.py", line=2, side="RIGHT"),
        ),
        round_id=4,
    )
    output = {
        "requests": [
            {"comment_ref": "c2", "status": "addressed", "explanation": "Fixed.",
             "hunks": [{"path": "app.py", "hunk": 1}, {"path": "ghost.py", "hunk": 0},
                       {"path": "app.py", "hunk": 7}, {"path": "app.py"}, "x"]},
            {"comment_ref": "c3", "status": "partly", "explanation": "Half.", "hunks": []},
            {"comment_ref": "c1", "status": "not_addressed", "explanation": "No.", "hunks": []},
            {"comment_ref": "c9", "status": "addressed", "explanation": "Invented.", "hunks": []},
            {"comment_ref": "c3", "status": "addressed", "explanation": "Again.", "hunks": []},
        ],
        "new_changes_walkthrough": {
            "steps": [
                {"title": "New", "body": "b", "hunks": [{"path": "app.py", "hunk": 1}]},
                {"title": "Unchanged", "body": "b", "hunks": [{"path": "app.py", "hunk": 0}]},
                {"title": "Ghost", "body": "b", "hunks": [{"path": "ghost.py", "hunk": 0}]},
            ],
            "skipped": [{"path": "app.py", "hunk": 0, "reason": "format_only"}],
        },
    }  # fmt: skip

    artifact = rereview_artifact(output, previous, diff, (HunkRef("app.py", 1),))

    requests = artifact["requests"]
    assert isinstance(requests, list)
    assert [(r["comment_ref"], r["status"]) for r in requests] == [
        ("c2", "addressed"),
        ("c3", "partly"),
        ("c1", "not_addressed"),
    ]
    # Hunks are kept only where the diff has them (Jesinia's numbering, never renumbered).
    assert artifact["requests"][0]["hunks"] == [{"path": "app.py", "hunk": 1}]  # type: ignore[index]
    assert artifact["unanswered"] == []
    assert artifact["dropped_requests"] == 2  # an unknown ref and a repeat
    walk = artifact["new_changes_walkthrough"]
    assert [s["title"] for s in walk["steps"]] == ["New"]  # type: ignore[index]
    assert artifact["new_hunks"] == [{"path": "app.py", "hunk": 1}]
    cov = artifact["coverage"]
    assert isinstance(cov, dict) and cov["pct"] == 100.0 and cov["lines_total"] == 2
    assert {"path": "app.py", "hunk": 0, "reason": "unchanged"} in cov["not_walked"]
    assert artifact["previous_round_id"] == 4

    # A comment the agent gave no status is listed as unanswered, never guessed.
    some = {**output, "requests": output["requests"][:1]}  # type: ignore[index]
    assert rereview_artifact(some, previous, diff, (HunkRef("app.py", 1),))["unanswered"] == [
        "c1",
        "c3",
    ]


async def test_commented_only_prs_get_incremental_rereview(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    inline = {"path": "app.py", "line": 30, "side": "RIGHT", "body": "Off by one here.",
              "start_line": None, "start_side": None}  # fmt: skip
    commented = env.posted_round(repo.head, repo.base, "COMMENT", "A few notes.", [inline])
    env.sync(repo.pushed, repo.base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    followup = await env.followups.classify(stored)
    assert followup.kind == "rereview" and followup.prev_source == "round"
    assert followup.prev_round_id == commented and followup.prev_merge_base == repo.base

    result = await env.pipeline.open(env.ref)
    await env.pipeline.drain()

    assert JobKind.REREVIEW in result.jobs and result.round_id is not None
    given = _rereview_round(env).rereview  # type: ignore[attr-defined]
    assert given.previous.source == "round" and given.previous.round_id == commented
    assert [(c.ref, c.path, c.line, c.body) for c in given.previous.comments] == [
        ("c1", None, None, "A few notes."),
        ("c2", "app.py", 30, "Off by one here."),
    ]
    # Only the hunk that changed since the comment is new: line 5's hunk is as reviewed, and
    # lib.py's hunk followed its rename to pkg_lib.py unchanged.
    assert given.new_hunks == (HunkRef("app.py", 1),)
    assert "line thirty, fixed" in given.new_changes and "line five" not in given.new_changes
    artifacts = env.store.artifacts(result.round_id)
    previous = artifacts["previous_review"]
    assert isinstance(previous, dict) and previous["source"] == "round"
    assert previous["comments"][1] == {
        "ref": "c2", "body": "Off by one here.", "path": "app.py", "line": 30,
        "side": "RIGHT", "start_line": None, "resolved": False,
    }  # fmt: skip
    rereview = artifacts["rereview"]
    assert isinstance(rereview, dict)
    assert [(r["comment_ref"], r["status"]) for r in rereview["requests"]] == [("c2", "addressed")]
    assert rereview["unanswered"] == ["c1"]
    assert [s["title"] for s in rereview["new_changes_walkthrough"]["steps"]] == ["The fix"]
    jobs = artifacts["jobs"]
    assert isinstance(jobs, dict) and jobs["rereview"] == {"state": "done", "error": None}
    # The round's own diff is the whole PR (a commented review's re-review reads it all).
    index = env.jobs.rounds[0].hunk_index
    assert index is not None and "pkg_lib.py" in index.text and "unchanged" not in index.text

    # Opening again runs nothing more: the re-review is done for this head.
    again = await env.pipeline.open(env.ref)
    assert again.state == "up_to_date"


async def test_rereview_without_stored_walkthrough_uses_github_comments(env: Env) -> None:
    repo = env.repo
    # Changes requested on GitHub by hand: no stored round, no marker.
    env.github.reviews_of[(REPO, 1)] = [
        ReviewSummary("me", False, "CHANGES_REQUESTED", repo.head, START, 2, "Please fix these."),
    ]
    env.github.threads_of[(REPO, 1)] = [
        ReviewThread("app.py", 30, "Off by one here.", "me", False, repo.head),
        ReviewThread("app.py", 5, "Someone else's nit.", "alice", False, repo.head),
        ReviewThread("lib.py", 3, "Already sorted.", "Me", True, repo.head),
    ]
    env.sync(repo.pushed, repo.base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    followup = await env.followups.classify(stored)
    assert followup.kind == "rereview" and followup.prev_source == "github"
    assert followup.prev_round_id is None

    previous = await env.rereviews.previous_comments(env.ref, followup)

    assert previous.source == "github" and previous.round_id is None
    assert previous.head_sha == repo.head
    assert [(c.ref, c.path, c.line, c.body, c.resolved) for c in previous.comments] == [
        ("c1", None, None, "Please fix these.", False),
        ("c2", "app.py", 30, "Off by one here.", False),
        ("c3", "lib.py", 3, "Already sorted.", True),
    ]
    # The pipeline hands them to the re-review job, and keeps them with the round.
    result = await env.pipeline.open(env.ref)
    await env.pipeline.drain()
    assert result.round_id is not None
    assert _rereview_round(env).rereview.previous.source == "github"  # type: ignore[attr-defined]
    stored_previous = env.store.artifact(result.round_id, "previous_review")
    assert isinstance(stored_previous, dict) and stored_previous["source"] == "github"

    # A stored round that is gone (pruned) falls back to GitHub the same way.
    gone = replace(followup, prev_source="round", prev_round_id=999)
    assert (await env.rereviews.previous_comments(env.ref, gone)).source == "github"


# --- R69: per-hunk filtering ------------------------------------------------------------------


async def test_a_changed_rebase_reviews_only_the_hunks_that_changed(env: Env) -> None:
    repo = env.repo
    # Approved by hand on GitHub (no stored round).
    env.github.reviews_of[(REPO, 1)] = [
        ReviewSummary("me", False, "APPROVED", repo.head, START, 0, "LGTM")
    ]
    env.sync(repo.rebased, repo.rebased_base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    followup = await env.followups.classify(stored)
    assert followup.kind == "changed_hunks_only" and followup.differing_paths == ["app.py"]

    result = await env.pipeline.open(env.ref)
    await env.pipeline.drain()
    assert result.round_id is not None

    # app.py keeps both hunks in R42's numbering; hunk 0 (line 5) is marked, not dropped.
    index = env.jobs.rounds[0].hunk_index
    assert index is not None and "lib.py" not in index.text
    hunk0, hunk1 = (line for line in index.text.splitlines() if line.lstrip().startswith("hunk"))
    assert hunk0.lstrip().startswith("hunk 0:") and "unchanged since your last review" in hunk0
    assert hunk1.lstrip().startswith("hunk 1:") and "unchanged" not in hunk1
    coverage_artifact = env.store.artifact(result.round_id, "coverage")
    assert isinstance(coverage_artifact, dict)
    walk = coverage_artifact["walkthrough"]
    # The scripted walkthrough cites hunk 0 of src/app.py (not in this diff): nothing walked,
    # and only hunk 1's two lines count.
    assert walk["lines_total"] == 2
    assert {"path": "app.py", "hunk": 0, "reason": "unchanged"} in walk["not_walked"]
    warnings = env.store.artifact(result.round_id, "warnings")
    assert isinstance(warnings, list) and any("1 of 2 hunks" in w for w in warnings)


async def test_unknown_changes_count_every_hunk_as_new(env: Env) -> None:
    """Fail closed: when the previous diff can't be rebuilt, nothing is marked unchanged."""
    repo = env.repo
    diff = parse_unified_diff(await stored_diff(env, repo.base, repo.pushed))
    every = {HunkRef(f.path, h) for f in diff.files for h in range(len(f.hunks))}
    unknown = FollowUp("rereview", [], "n", repo.head, "round")  # no merge base recorded
    assert await env.rereviews.changed_hunks(env.ref, unknown, repo.pushed, diff) == every
    gone = FollowUp("rereview", [], "n", "d" * 40, "github", prev_merge_base="e" * 40)
    with pytest.raises(Exception):  # noqa: B017 - the workspace's own error, whatever it is
        await env.rereviews.changed_hunks(env.ref, gone, repo.pushed, diff)


# --- Task 11 M3: drafts follow renames ---------------------------------------------------------


async def test_a_draft_on_a_renamed_file_reanchors_to_its_new_path(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    old = env.store.start_round(env.ref, repo.head, repo.base, "quick", env.clock())
    env.store.save_drafts(
        old,
        "",
        [
            Draft("lib.py", "On the renamed file.", line=3, side="RIGHT"),
            Draft("lib.py", "Base side of the renamed file.", line=3, side="LEFT"),
        ],
    )
    env.sync(repo.pushed, repo.base)
    new = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())

    assert await env.followups.reanchor(new, repo.pushed, repo.base) == 2

    right, left = env.store.drafts(new)
    assert (right.path, right.line, right.outdated) == ("pkg_lib.py", 3, False)
    # The base is the same commit: lib.py's old side is still lib.py there.
    assert (left.path, left.line, left.outdated) == ("lib.py", 3, False)


# --- Task 11 M7: fetch the new head, then open it under the normal gates ----------------------


async def test_refresh_fetches_and_classifies_the_new_head_without_carrying_review_anyway(
    env: Env,
) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    inline = {"path": "app.py", "line": 30, "side": "RIGHT", "body": "Off by one.",
              "start_line": None, "start_side": None}  # fmt: skip
    env.posted_round(repo.head, repo.base, "COMMENT", "Notes.", [inline])
    env.store.override_gates(env.ref, repo.head)  # Review anyway, on the old head
    ticks: list[str] = []

    async def changed() -> None:
        ticks.append("tick")

    queue = QueueService(
        env.github, env.store, env.clock, on_change=changed, on_refresh=env.followups.check
    )
    # GitHub has moved on; the cache hasn't synced yet.
    env.github.inbox = [pull(1, REPO, head=repo.pushed, base=repo.base)]
    env.github.ci[repo.pushed] = "failing"
    env.pipeline_github.pr = replace(env.github.inbox[0], commit_count=1)

    row = await queue.refresh(env.ref)

    assert row.head_sha == repo.pushed and row.ci_state == "failing"
    assert row.followup_kind == "rereview"  # classified now, not on a later sync
    assert ticks == ["tick"]
    # Opening the new head applies its own gates: the old head's Review anyway doesn't carry.
    result = await env.pipeline.open(env.ref)
    assert result.state == "ci_failing" and result.round_id is None

    with pytest.raises(PrNotInQueueError):
        await queue.refresh(PrRef(REPO, 2))
    env.github.inbox = [pull(1, REPO, head=repo.pushed, base=repo.base, state="closed")]
    with pytest.raises(PrNotOpenError):
        await queue.refresh(env.ref)


async def test_a_failed_check_on_refresh_is_recorded_unchecked(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    env.github.inbox = [pull(1, REPO, head=repo.pushed, base=repo.base)]
    queue = QueueService(env.github, env.store, env.clock, on_refresh=env.followups.check)
    calls = {"n": 0}

    async def flaky(repo_name: str, number: int) -> list[ReviewSummary]:
        calls["n"] += 1
        if calls["n"] > 1:  # the queue's own read works; the follow-up check's doesn't
            raise GitHubRequestError(502, "Bad Gateway")
        return []

    env.github.reviews = flaky  # type: ignore[method-assign,assignment]
    row = await queue.refresh(env.ref)
    assert row.head_sha == repo.pushed and row.kind == "new" and row.followup_kind is None
    assert "Couldn't check this head" in row.followup_note
    known = env.store.followup(env.ref, repo.pushed)
    assert known is not None and known.error is not None and "502" in known.error


# --- failures stay with the re-review job -------------------------------------------------------


async def test_unreadable_previous_comments_fail_only_the_rereview_job(env: Env) -> None:
    repo = env.repo
    env.github.reviews_of[(REPO, 1)] = [
        ReviewSummary("me", False, "CHANGES_REQUESTED", repo.head, START, 1, "Fix it."),
    ]
    env.sync(repo.pushed, repo.base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    await env.followups.classify(stored)

    async def down(repo_name: str, number: int) -> list[ReviewThread]:
        raise GitHubRequestError(502, "Bad Gateway")

    env.github.threads = down  # type: ignore[method-assign,assignment]
    result = await env.pipeline.open(env.ref)
    await env.pipeline.drain()
    assert result.round_id is not None
    jobs = env.store.artifact(result.round_id, "jobs")
    assert isinstance(jobs, dict) and jobs["rereview"]["state"] == "failed"
    assert "couldn't be read from GitHub" in jobs["rereview"]["error"]
    assert "502" in jobs["rereview"]["error"]  # the cause is kept
    assert jobs["overview"]["state"] == "done"  # the other jobs ran
    assert JobKind.REREVIEW not in env.jobs.ran
    rnd = env.store.round(result.round_id)
    assert rnd is not None and rnd.finished_at is not None and rnd.error is None


async def test_an_ordinary_new_head_gets_no_rereview(env: Env) -> None:
    repo = env.repo
    env.sync(repo.pushed, repo.base)
    result = await env.pipeline.open(env.ref)
    await env.pipeline.drain()
    assert result.round_id is not None
    assert JobKind.REREVIEW not in result.jobs and JobKind.REREVIEW not in env.jobs.ran
    assert all(r.rereview is None for r in env.jobs.rounds)
    assert env.store.artifact(result.round_id, "previous_review") is None


# --- the domain rules ---------------------------------------------------------------------------


def test_previous_comments_are_read_from_postings_and_threads() -> None:
    posted = comments_from_posting(
        "Summary.",
        [
            {"path": "a.py", "line": 3, "side": "RIGHT", "body": "One.", "start_line": 1,
             "start_side": "RIGHT"},
            {"path": "a.py", "line": "x", "side": "UP", "body": "Bad shape."},
            {"path": "", "line": 1, "side": "RIGHT", "body": "No path."},
            "not a comment",
        ],
    )  # fmt: skip
    assert [(c.ref, c.path, c.line, c.start_line, c.side) for c in posted] == [
        ("c1", None, None, None, None),
        ("c2", "a.py", 3, 1, "RIGHT"),
        ("c3", "a.py", None, None, None),  # a malformed line is dropped, the text kept
    ]
    assert comments_from_posting("  ", []) == []
    threads = comments_from_threads(
        [
            ReviewThread("a.py", None, "File-level.", "me", False, None),
            ReviewThread("a.py", 2, "   ", "me", False, None),
            ReviewThread("a.py", 2, "Not mine.", "bob", False, None),
        ],
        "ME",
        "",
    )
    assert [(c.ref, c.path, c.line, c.side) for c in threads] == [("c1", "a.py", None, None)]
    assert PreviousComment("c1", "x", path="a.py", line=2, side="LEFT").label() == "a.py:2 (old)"
    assert PreviousComment("c1", "x", path="a.py", line=2, start_line=1).label() == "a.py:1–2"
    assert PreviousComment("c1", "x", path="a.py").label() == "a.py"
    assert PreviousComment("c1", "x").label() == "the review"
    for bad in (
        {"ref": "", "body": "x"},
        {"ref": "c1", "body": " "},
        {"ref": "c1", "body": "x", "line": 0},
        {"ref": "c1", "body": "x", "path": "a.py", "side": "UP"},
    ):
        with pytest.raises(InvalidPreviousCommentError):
            PreviousComment(**bad)


def test_hunks_match_by_content_across_renames_and_moves() -> None:
    old = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,2 +10,2 @@\n x\n-y\n+Y\n"
        "diff --git a/old.py b/old.py\n--- a/old.py\n+++ b/old.py\n@@ -1 +1 @@\n-p\n+q\n"
    )
    new = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -3,2 +3,2 @@\n a\n-b\n+B\n@@ -12,2 +12,2 @@\n x\n-y\n+Z\n"
        "diff --git a/new.py b/new.py\n--- a/new.py\n+++ b/new.py\n@@ -1 +1 @@\n-p\n+q\n"
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n-p\n+q\n"
    )
    # Moved lines (other numbers) are unchanged; a whitespace-free edit is not.
    assert changed_hunks(old, new, {"old.py": "new.py"}) == {
        HunkRef("a.py", 1),
        HunkRef("c.py", 0),  # same text, but no rename ties it to old.py
    }
    assert changed_hunks(None, new, {}) == {
        HunkRef(f.path, h) for f in new.files for h in range(len(f.hunks))
    }
    # Whitespace counts: a re-indented line is a change.
    spaced = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n a\n-b\n+ B\n"
    )
    assert changed_hunks(old, spaced, {}) == {HunkRef("a.py", 0)}


def test_marked_hunks_keep_their_numbers_and_leave_coverage() -> None:
    diff = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,2 +10,2 @@\n x\n-y\n+Y\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-p\n+q\n"
    )
    marked = only_hunks(diff, [HunkRef("a.py", 1)])
    assert marked.changed_lines == 2 and diff.changed_lines == 6
    assert marked.locate("a.py", 0) == (0, 0)  # still citable by its number
    index = hunk_index(marked).text
    assert "hunk 0:" in index and "hunk 1:" in index and index.count("unchanged") == 2
    walked = coverage(marked, [Step((HunkRef("a.py", 1),))], [])
    assert walked.pct == 100.0 and walked.files_covered == 1 and walked.files_total == 1
    assert walked.not_walked == [Skip("a.py", 0, "unchanged"), Skip("b.py", None, "unchanged")]
    # Citing an unchanged hunk adds nothing; leaving the new one out is still "omitted".
    other = coverage(marked, [Step((HunkRef("a.py", 0),))], [])
    assert other.lines_covered == 0 and Skip("a.py", 1, "omitted") in other.not_walked
    # An incomplete diff can't be told apart: nothing is marked.
    partial = replace(diff, complete=False)
    assert only_hunks(partial, []) is partial


def test_new_changes_text_and_comment_listing_are_capped_with_jesinias_notes() -> None:
    diff = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,2 +10,2 @@\n x\n-y\n+Y\n",
        keep_text=True,
    )
    shown, cut = new_changes_text(diff, [HunkRef("a.py", 1), HunkRef("gone.py", 0)])
    assert cut == 0 and '"a.py" hunk 1' in shown and "+Y" in shown and "+B" not in shown
    shown, cut = new_changes_text(diff, [HunkRef("a.py", 0), HunkRef("a.py", 1)], limit=40)
    assert cut == 1 and "+B" in shown and "+Y" not in shown
    assert new_changes_text(diff, []) == ("No hunk changed since your last review.", 0)
    comments = [PreviousComment(f"c{i}", "x" * 30, path="a.py", line=i) for i in range(1, 4)]
    listed, cut = comments_listing(comments)
    assert cut == 0 and "[c1] a.py:1" in listed and "[c3]" in listed
    listed, cut = comments_listing(comments, limit=60)
    assert cut == 2 and "[c2]" not in listed
    assert comments_listing([]) == ("Your previous review left no comments.", 0)


def test_the_rereview_schema_takes_only_the_three_statuses() -> None:
    schema = schema_for(JobKind.REREVIEW)
    assert parse_job_output(json.dumps(REREVIEW_OUTPUT), schema) == REREVIEW_OUTPUT
    bad = json.loads(json.dumps(REREVIEW_OUTPUT))
    bad["requests"][0]["status"] = "partly_addressed"
    with pytest.raises(InvalidAgentOutput, match="enum"):
        parse_job_output(json.dumps(bad), schema)
    with pytest.raises(InvalidAgentOutput, match="new_changes_walkthrough"):
        parse_job_output(json.dumps({"requests": []}), schema)


async def test_degraded_inputs_count_every_hunk_as_new_and_say_why(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    inline = {"path": "app.py", "line": 30, "side": "RIGHT", "body": "Off by one.",
              "start_line": None, "start_side": None}  # fmt: skip
    older = env.posted_round(repo.head, repo.base, "COMMENT", "Older notes.", [])
    env.posted_round(repo.head, repo.base, "COMMENT", "Notes.", [inline])
    env.sync(repo.pushed, repo.base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    followup = await env.followups.classify(stored)
    # An older posted round is still the one a follow-up names when it is the previous one.
    named = await env.rereviews.previous_comments(env.ref, replace(followup, prev_round_id=older))
    assert named.round_id == older and [c.body for c in named.comments] == ["Older notes."]

    # The previous diff is too large to read: every hunk is new, nothing hidden.
    diff = parse_unified_diff(await stored_diff(env, repo.base, repo.pushed))
    real_diff = env.workspace.diff

    async def too_large(name: str, base: str, head: str) -> str:
        if head == repo.head:
            raise DiffTooLargeError("over the cap", 1)
        return await real_diff(name, base, head)

    env.workspace.diff = too_large  # type: ignore[method-assign,assignment]
    every = {HunkRef(f.path, h) for f in diff.files for h in range(len(f.hunks))}
    assert await env.rereviews.changed_hunks(env.ref, followup, repo.pushed, diff) == every
    env.workspace.diff = real_diff  # type: ignore[method-assign]

    # The new changes can't be read again: the re-review's input fails typed, with its cause.
    with pytest.raises(RereviewInputError, match="new changes couldn't be read"):
        await env.rereviews.new_changes(env.ref, "d" * 40, repo.pushed, frozenset(every))

    # Working out what changed fails in the round: a warning, and every hunk is new.
    async def broken(*args: object) -> frozenset[HunkRef]:
        raise GitCommandError(("git", "diff"), 128, "bad object")

    env.rereviews.changed_hunks = broken  # type: ignore[method-assign,assignment]
    result = await env.pipeline.open(env.ref)
    await env.pipeline.drain()
    assert result.round_id is not None
    warnings = env.store.artifact(result.round_id, "warnings")
    assert isinstance(warnings, list) and any("every hunk counts as new" in w for w in warnings)
    given = _rereview_round(env).rereview  # type: ignore[attr-defined]
    assert set(given.new_hunks) == every
    await env.followups.check(PrRef(REPO, 99))  # an unknown PR: nothing to check


async def test_a_rereview_planned_on_open_fails_when_its_follow_up_is_gone(
    conn: sqlite3.Connection,
) -> None:
    """The follow-up can go between opening and preparing (a posting clears it): the
    re-review job then fails with that cause, and the rest of the round runs."""
    store = SqliteReviewStore(conn)
    store.upsert_repo(RepoConfig(REPO, watched=True))
    clock = FakeClock()
    github = FakeGitHub(replace(pull(1), body="", commit_count=1))
    jobs = FakeJobs(store)

    class Vanishing:
        calls = 0

        async def reanchor(self, round_id: int, head_sha: str, merge_base: str) -> int:
            return 0

        def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None:
            self.calls += 1
            return FollowUp("rereview", [], "n", "c" * 40, "github") if self.calls == 1 else None

    pipeline = Pipeline(
        github=github,
        files=FakeFiles(),
        workspace=FakeWorkspace(),
        jobs=jobs,
        store=store,
        bus=EventBus(),
        clock=clock,
        followups=Vanishing(),
        rereviews=Rereviews(github=FakePullRequests(), workspace=FakeWorkspace(), store=store),  # type: ignore[arg-type]
    )
    snapshot = snapshot_of(
        github.pr, ci_state="passing", reviews=[], automated_reviewer=None, viewer="me"
    )
    store.apply_sync([snapshot], [], [], clock() - timedelta(minutes=20))
    result = await pipeline.open(snapshot.ref)
    await pipeline.drain()
    assert JobKind.REREVIEW in result.jobs and result.round_id is not None
    jobs_record = store.artifact(result.round_id, "jobs")
    assert isinstance(jobs_record, dict) and jobs_record["rereview"]["state"] == "failed"
    assert "no previous review to build on" in jobs_record["rereview"]["error"]
    assert JobKind.REREVIEW not in jobs.ran


def test_a_hunk_without_a_final_newline_keeps_its_marker_in_digest_and_text() -> None:
    with_marker = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
        "\\ No newline at end of file\n",
        keep_text=True,
    )
    without = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    (hunk,) = with_marker.files[0].hunks
    assert hunk.text is not None and hunk.text.endswith("\\ No newline at end of file")
    assert hunk.digest != without.files[0].hunks[0].digest
    assert without.files[0].hunks[0].text is None  # kept only when asked
