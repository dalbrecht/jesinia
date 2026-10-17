# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebases and head changes (plan Task 11, spec §4.1 and §7 "When the head changes").

Fixture repos are real git in tmp dirs (``GIT_CONFIG_GLOBAL=/dev/null``), fetched into the
real ``GitWorkspace``; ``review.sqlite`` is real; GitHub is a double (no network).
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

from jesinia.apps.review.api.routes import _error
from jesinia.apps.review.application.followup import FollowUps
from jesinia.apps.review.application.pipeline import Pipeline
from jesinia.apps.review.application.posting import PostingService
from jesinia.apps.review.application.queue import QueueService
from jesinia.apps.review.application.reviews import ReviewReads
from jesinia.apps.review.application.scheduler import Scheduler
from jesinia.apps.review.domain.coverage import parse_unified_diff
from jesinia.apps.review.domain.followup import (
    FollowUp,
    InvalidPrevReviewError,
    LineMaps,
    PrevReview,
    classify,
    differing_paths,
    line_map,
    pr_kind,
    reanchor,
    restrict_diff,
    verdict_of_event,
    verdict_of_review_state,
)
from jesinia.apps.review.domain.posting import (
    Draft,
    ReviewedHeadGone,
    RoundChanged,
    RoundFrozen,
    commentable_lines,
    compose_review,
)
from jesinia.apps.review.domain.queue import PrRef, QueueFilter, RepoConfig, SyncStatus, snapshot_of
from jesinia.apps.review.domain.workspace import GitCommandError, MirrorFileTooLargeError
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.infrastructure.workspace import GitWorkspace
from jesinia.apps.review.module import REVIEW_DB
from jesinia.core.db import open_db
from jesinia.core.events import EventBus
from jesinia.services.github.application.service import GitHubRequestError
from jesinia.services.github.domain.pr import PrFile, ReviewSummary
from tests.apps.review.fakes import START, FakeClock, FakePullRequests, pull
from tests.apps.review.test_pipeline import FakeFiles, FakeGitHub, FakeJobs, FakeWorkspace
from tests.apps.review.test_posting import FakeGitHub as PostingGitHub
from tests.apps.review.test_posting import FileSource

REPO = "acme/web"
LINES = [f"line {i}" for i in range(1, 31)]
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40
MARKER_REVIEW = "Looks good.\n\n<!-- jesinia-posting:{} -->"


# --- fixture repos ------------------------------------------------------------------------------


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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(work: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (work / name).write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


def text(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


@dataclass(frozen=True)
class Repo:
    """A PR approved at ``head``, then rebased cleanly (``rebased``), rebased over a conflict
    (``conflict``), and pushed to with lines moved and one changed (``pushed``)."""

    remote: Path
    base: str
    head: str
    rebased_base: str
    rebased: str
    conflict_base: str
    conflict: str
    pushed: str


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "main")
    base = commit(work, {"app.py": text(LINES), "other.txt": "other\n"}, "base")

    git(work, "checkout", "-q", "-b", "feature")
    approved = [*LINES]
    approved[9] = "line ten"  # line 10
    head = commit(work, {"app.py": text(approved), "new.txt": "new file\n"}, "feature")

    git(work, "checkout", "-q", "-b", "main2", base)
    rebased_base = commit(work, {"other.txt": "other, changed\n"}, "unrelated")
    git(work, "checkout", "-q", "-b", "rebased", head)
    git(work, "rebase", "-q", "main2")
    rebased = git(work, "rev-parse", "HEAD")

    git(work, "checkout", "-q", "-b", "main3", base)
    conflicted = [*LINES]
    conflicted[9] = "line 10 (main)"
    conflict_base = commit(work, {"app.py": text(conflicted)}, "conflicting")
    git(work, "checkout", "-q", "-b", "conflict", conflict_base)
    resolved = [*LINES]
    resolved[9] = "line ten (main)"
    conflict = commit(work, {"app.py": text(resolved), "new.txt": "new file\n"}, "resolved")

    git(work, "checkout", "-q", "feature")
    moved = ["added 1", "added 2", *approved]
    moved[13] = "line twelve"  # was line 12, now line 14
    pushed = commit(work, {"app.py": text(moved)}, "more")

    for branch in ("main", "feature", "main2", "rebased", "main3", "conflict"):
        git(work, "push", "-q", "origin", branch)
    return Repo(remote, base, head, rebased_base, rebased, conflict_base, conflict, pushed)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, REVIEW_DB)
    yield connection
    connection.close()


@dataclass
class Env:
    store: SqliteReviewStore
    github: FakePullRequests
    workspace: GitWorkspace
    clock: FakeClock
    followups: FollowUps
    repo: Repo
    ref: PrRef

    def sync(self, head: str, base: str, *, ci: str = "passing") -> None:
        """The PR as a sync caches it, at ``head`` on ``base``."""
        pr = pull(1, REPO, head=head, base=base)
        self.github.inbox = [pr]
        snapshot = snapshot_of(
            pr,
            ci_state=ci,  # type: ignore[arg-type]
            reviews=[],
            automated_reviewer=None,
            viewer="me",
        )
        self.store.apply_sync([snapshot], [], [], self.clock() - timedelta(minutes=20))

    def approved_round(self, head: str, base: str) -> int:
        """A round on ``head`` whose APPROVE Jesinia posted."""
        round_id = self.store.start_round(self.ref, head, base, "quick", self.clock())
        self.store.finish_round(round_id, self.clock(), None, "approve")
        posting = self.store.start_posting(
            round_id, "APPROVE", "k", "d", "Looks good.", [], self.clock()
        )
        self.store.finish_posting(posting.id, "https://github.com/acme/web/pull/1#r1")
        self.github.reviews_of[(REPO, 1)] = [
            ReviewSummary(
                "me", False, "APPROVED", head, START, 0, MARKER_REVIEW.format(posting.id), "u"
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
    return Env(store, github, workspace, clock, followups, repo, PrRef(REPO, 1))


async def stored_diff(env: Env, base: str, head: str) -> str:
    await env.workspace.ensure_mirror(REPO)
    await env.workspace.fetch(REPO, [base, head])
    return await env.workspace.diff(REPO, base, head)


# --- the brief's tests --------------------------------------------------------------------------


async def test_pure_rebase_is_still_approved(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    env.approved_round(repo.head, repo.base)
    env.sync(repo.rebased, repo.rebased_base)
    stored = env.store.pr(env.ref)
    assert stored is not None

    followup = await env.followups.classify(stored)

    assert followup.kind == "still_approved" and followup.differing_paths == []
    assert followup.prev_head == repo.head and followup.prev_source == "round"
    assert "still approved" in followup.note.lower()
    # The queue files it under Still approved (kind "rebase"), with its one-line note…
    queue = QueueService(env.github, env.store, env.clock)
    (row,) = (await queue.list(QueueFilter())).rows
    assert row.kind == "rebase" and row.followup_kind == "still_approved"
    assert row.followup_note == followup.note and row.differing_paths == ()
    # …and no pre-review runs for it: no action is needed (spec §4.1).
    ran: list[object] = []

    async def run(request: object) -> None:
        ran.append(request)

    scheduler = Scheduler(env.store, lambda: env.clock() + timedelta(hours=1), run=run)
    await scheduler.tick()
    await scheduler.drain()
    assert ran == []
    stored = env.store.pr(env.ref)
    assert stored is not None and stored.state == "not_started"
    assert "still approved" in stored.state_reason
    # A loaded PR is the author asking for a review now: it runs.
    env.store.set_loaded(env.ref)
    await scheduler.tick()
    await scheduler.drain()
    assert len(ran) == 1

    # Same content but failing CI is not still approved: it is reviewed again.
    env.sync(repo.rebased, repo.rebased_base, ci="failing")
    stored = env.store.pr(env.ref)
    assert stored is not None
    failing = await env.followups.classify(stored)
    assert failing.kind == "rereview" and "failing" in failing.note


async def test_conflict_edit_reviews_only_differing_hunks(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    env.approved_round(repo.head, repo.base)
    env.sync(repo.conflict, repo.conflict_base)
    stored = env.store.pr(env.ref)
    assert stored is not None

    followup = await env.followups.classify(stored)

    assert followup.kind == "changed_hunks_only"
    assert followup.differing_paths == ["app.py"]  # new.txt is the approved new.txt
    assert env.followups.followup(env.ref, repo.conflict) == followup
    queue = QueueService(env.github, env.store, env.clock)
    (row,) = (await queue.list(QueueFilter())).rows
    assert row.kind == "rereview" and row.differing_paths == ("app.py",)

    # The round on the new head reviews only the differing files: its diff, so its hunk index
    # and coverage, hold app.py alone.
    diff = parse_unified_diff(await stored_diff(env, repo.conflict_base, repo.conflict))
    assert {f.path for f in diff.files} == {"app.py", "new.txt"}
    assert [f.path for f in restrict_diff(diff, followup.differing_paths).files] == ["app.py"]


async def test_conflict_edit_restricts_the_rounds_diff_in_the_pipeline(
    conn: sqlite3.Connection,
) -> None:
    store = SqliteReviewStore(conn)
    store.upsert_repo(RepoConfig(REPO, watched=True))
    clock = FakeClock()
    github = FakeGitHub(replace(pull(1), body="", commit_count=1))
    jobs = FakeJobs(store)

    class Changed:
        reanchored: list[tuple[int, str, str]] = []

        async def reanchor(self, round_id: int, head_sha: str, merge_base: str) -> int:
            self.reanchored.append((round_id, head_sha, merge_base))
            return 0

        def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None:
            return FollowUp("changed_hunks_only", ["src/app.py"], "1 file changed since", SHA_C)

    changed = Changed()
    scheduler = Scheduler(store, clock)
    pipeline = Pipeline(
        github=github,
        files=FakeFiles(),
        workspace=FakeWorkspace(),
        jobs=jobs,
        store=store,
        bus=EventBus(),
        clock=clock,
        slots=scheduler,
        followups=changed,
    )
    snapshot = snapshot_of(
        github.pr, ci_state="passing", reviews=[], automated_reviewer=None, viewer="me"
    )
    store.apply_sync([snapshot], [], [], clock() - timedelta(minutes=20))
    result = await pipeline.open(snapshot.ref)
    await pipeline.drain()
    assert result.round_id is not None
    index = jobs.rounds[0].hunk_index
    assert index is not None and "src/app.py" in index.text and "uv.lock" not in index.text
    artifacts = store.artifacts(result.round_id)
    assert artifacts["followup"] == {
        "kind": "changed_hunks_only",
        "differing_paths": ["src/app.py"],
        "note": "1 file changed since",
        "prev_head": SHA_C,
    }
    warnings = artifacts["warnings"]
    assert isinstance(warnings, list) and any("1 file" in w for w in warnings)
    assert changed.reanchored and changed.reanchored[0][0] == result.round_id


async def test_github_posted_approval_counts(env: Env) -> None:
    repo = env.repo
    # Approved on GitHub by hand (before Jesinia, say): no stored round, no marker.
    env.github.reviews_of[(REPO, 1)] = [
        ReviewSummary("alice", False, "APPROVED", repo.head, START, 0),  # not the author's
        ReviewSummary("Me", False, "APPROVED", repo.head, START, 0, "LGTM"),
    ]
    env.sync(repo.rebased, repo.rebased_base)
    stored = env.store.pr(env.ref)
    assert stored is not None

    prev = await env.followups.previous_review(stored)
    assert prev == PrevReview(repo.head, repo.base, "approved", "github")
    followup = await env.followups.classify(stored)
    assert followup.kind == "still_approved" and followup.prev_source == "github"

    # The author's latest review decides: a later request for changes is a re-review.
    env.github.reviews_of[(REPO, 1)].append(
        ReviewSummary("me", False, "CHANGES_REQUESTED", repo.head, START, 2, "Fix it")
    )
    assert (await env.followups.classify(stored)).kind == "rereview"
    # A dismissed review isn't the author's verdict any more; a pending one isn't posted.
    env.github.reviews_of[(REPO, 1)] = [
        ReviewSummary("me", False, "DISMISSED", repo.head, START, 0),
        ReviewSummary("me", False, "PENDING", repo.head, START, 0),
    ]
    assert await env.followups.previous_review(stored) is None
    assert (await env.followups.classify(stored)).kind == "new"


async def test_drafts_carry_and_reanchor(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    old = env.store.start_round(env.ref, repo.head, repo.base, "quick", env.clock())
    drafts = [
        Draft("app.py", "Unchanged line, moved down by two.", line=10, side="RIGHT"),
        Draft("app.py", "This line changes next push.", line=12, side="RIGHT"),
        Draft("app.py", "A range.", line=10, side="RIGHT", start_line=9, start_side="RIGHT"),
        Draft("app.py", "On the base side.", line=10, side="LEFT"),
        Draft("app.py", "A general note.", outside_diff=True),
    ]
    env.store.save_drafts(old, "My summary.", drafts)

    # The head moves and a round starts on it: the old round freezes, its drafts move over.
    env.sync(repo.pushed, repo.base)
    new = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    frozen = env.store.round(old)
    assert frozen is not None and frozen.frozen_at is not None and frozen.successor_id == new
    assert env.store.review_summary(new) == "My summary."
    carried = env.store.drafts(new)
    assert [d.body for d in carried] == [d.body for d in drafts]
    # Until re-anchored, every anchored draft is outdated: none can post inline by accident.
    assert [d.outdated for d in carried] == [True, True, True, True, False]

    reanchored = await env.followups.reanchor(new, repo.pushed, repo.base)

    assert reanchored == 4  # the general note has no line to move
    moved, changed, ranged, left, note = env.store.drafts(new)
    assert (moved.line, moved.side, moved.outdated) == (12, "RIGHT", False)
    assert (changed.line, changed.outdated) == (12, True)  # kept where it was, flagged
    assert (ranged.start_line, ranged.line, ranged.outdated) == (11, 12, False)
    assert (left.line, left.side, left.outdated) == (10, "LEFT", False)  # same merge base
    assert note.outside_diff and not note.outdated
    assert await env.followups.reanchor(new, repo.pushed, repo.base) == 0  # once

    # The frozen round is read-only: saves and posts are refused, typed, as a round change.
    service = PostingService(
        github=PostingGitHub(pull(1, REPO, head=repo.pushed)),
        writer=PostingGitHub(pull(1, REPO, head=repo.pushed)),
        files=FileSource(),
        store=env.store,
        clock=env.clock,
    )
    with pytest.raises(RoundFrozen) as refused:
        await service.save_drafts(old, "late edit", [])
    assert isinstance(refused.value, RoundChanged) and refused.value.successor_id == new
    assert env.store.review_summary(old) == "My summary."
    assert (await service.drafts(old)).drafts  # still readable from stored data

    # The outdated draft is never an inline comment without the author's choice: it posts in
    # the body, under its own heading, and the preview says so.
    patch = (await stored_diff(env, repo.base, repo.pushed)).split("@@", 1)[1]
    files = [PrFile("app.py", "modified", 4, 1, "@@" + patch, None)]
    review = compose_review("", env.store.drafts(new), commentable_lines(files))
    assert [c.body for c in review.comments] == [moved.body, ranged.body, left.body]
    assert "earlier version" in review.body and changed.body in review.body
    env.store.put_pr_files(env.ref, repo.pushed, files)
    preview = await service.preview(new, "COMMENT")
    assert any("outdated" in note for note in preview.notes)

    # Another head: the re-anchored drafts are pending again; the one already outdated has
    # nothing to re-anchor from. The author's save keeps a draft pending while its anchor is
    # as it was (an edited text included); changing the anchor is the author's choice.
    third = env.store.start_round(env.ref, repo.rebased, repo.rebased_base, "quick", env.clock())
    carried = env.store.drafts(third)
    assert [(d.body, h) for d, h, _ in env.store.carried_drafts(third)] == [
        (moved.body, repo.pushed),
        (ranged.body, repo.pushed),
        (left.body, repo.pushed),
    ]
    env.store.save_drafts(
        third,
        "",
        [
            replace(carried[0], outdated=False),
            carried[1],
            replace(carried[2], body="Edited range."),
            *carried[3:],
        ],
    )
    pending = env.store.carried_drafts(third)
    assert [d.body for d, _, _ in pending] == ["Edited range.", left.body]


async def test_post_refuses_when_reviewed_head_is_gone(env: Env) -> None:
    """Review Focus 1: a force-push rewrote history under an open review."""
    repo = env.repo
    env.sync(repo.head, repo.base)
    round_id = env.store.start_round(env.ref, repo.head, repo.base, "quick", env.clock())
    env.store.finish_round(round_id, env.clock(), None, "approve")
    env.store.save_drafts(round_id, "Looks good.", [])
    github = PostingGitHub(pull(1, REPO, head=repo.head), commits=[repo.head])
    service = PostingService(
        github=github, writer=github, files=FileSource(), store=env.store, clock=env.clock
    )
    shown = await service.preview(round_id, "APPROVE")

    # Force-pushed: the reviewed commit is no longer on the PR.
    github.pr = replace(github.pr, head_sha=repo.conflict)
    github.commits = [repo.conflict]
    with pytest.raises(ReviewedHeadGone, match="review the new head"):
        await service.post(round_id, "APPROVE", ["newer_head"], key="k", digest=shown.digest)
    assert github.posted == []
    # The round stays readable from stored data.
    assert (await service.drafts(round_id)).summary == "Looks good."

    # Once a round starts on the new head, the old one is read-only: refused, typed, even
    # where GitHub still lists the old commit.
    github.commits = [repo.head, repo.conflict]
    env.sync(repo.conflict, repo.conflict_base)
    env.store.start_round(env.ref, repo.conflict, repo.conflict_base, "quick", env.clock())
    with pytest.raises(RoundFrozen):
        await service.post(round_id, "APPROVE", ["newer_head"], key="k2", digest=shown.digest)
    with pytest.raises(RoundFrozen):
        await service.preview(round_id, "APPROVE")
    assert github.posted == []
    # At the store too: no posting can be recorded for a frozen round.
    with pytest.raises(RoundFrozen):
        env.store.start_posting(round_id, "APPROVE", "k3", "d", "", [], env.clock())


# --- the domain rules ---------------------------------------------------------------------------


def test_a_frozen_round_is_a_409_naming_its_successor() -> None:
    response = _error(RoundFrozen(3, 4))
    assert response.status_code == 409
    assert json.loads(bytes(response.body)) == {
        "error": "RoundFrozen",
        "detail": str(RoundFrozen(3, 4)),
        "successor_round_id": 4,
    }
    assert "round 4" in str(RoundFrozen(3, 4)) and "round" in str(RoundFrozen(3, None))


def _prev(verdict: str = "approved") -> PrevReview:
    return PrevReview(SHA_A, SHA_B, verdict, "round", 3)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _Pr:
    head_sha: str = SHA_C


def test_classify_rules() -> None:
    ids = {"a.py": "1", "b.py": "2"}
    assert classify(None, _Pr(), None, ids, "passing").kind == "new"
    # The head the author reviewed is no follow-up.
    assert classify(_prev(), _Pr(SHA_A), ids, ids, "passing").kind == "new"
    for ci in ("passing", "pending", "none"):
        assert classify(_prev(), _Pr(), ids, dict(ids), ci).kind == "still_approved"
    changed = classify(_prev(), _Pr(), ids, {"a.py": "1", "b.py": "9", "c.py": "3"}, "passing")
    assert changed.kind == "changed_hunks_only" and changed.differing_paths == ["b.py", "c.py"]
    assert "2 files" in changed.note
    one = classify(_prev(), _Pr(), ids, {"a.py": "1", "b.py": "9"}, "passing")
    assert "1 file " in one.note
    # Without the approved diff (force-pushed away, say) nothing is assumed unchanged.
    unknown = classify(_prev(), _Pr(), None, ids, "passing")
    assert unknown.kind == "rereview" and "couldn't" in unknown.note
    assert classify(_prev(), _Pr(), ids, None, "passing").kind == "rereview"
    for verdict in ("changes_requested", "commented"):
        again = classify(_prev(verdict), _Pr(), ids, {"a.py": "x", "b.py": "2"}, "passing")
        assert again.kind == "rereview" and again.differing_paths == ["a.py"]
    assert classify(_prev("commented"), _Pr(), None, None, "passing").differing_paths == []
    assert pr_kind(None) == "new"
    assert [pr_kind(FollowUp(k)) for k in ("new", "still_approved", "rereview")] == [
        "new",
        "rebase",
        "rereview",
    ]
    assert pr_kind(FollowUp("changed_hunks_only", ["a"])) == "rereview"


def test_differing_paths_and_verdicts() -> None:
    assert differing_paths({"a": "1", "gone": "2"}, {"a": "1", "new": "3"}) == ["gone", "new"]
    assert verdict_of_review_state("APPROVED") == "approved"
    assert verdict_of_review_state("CHANGES_REQUESTED") == "changes_requested"
    assert verdict_of_review_state("COMMENTED") == "commented"
    assert verdict_of_review_state("DISMISSED") is None
    assert verdict_of_event("APPROVE") == "approved"
    assert verdict_of_event("REQUEST_CHANGES") == "changes_requested"
    assert verdict_of_event("COMMENT") == "commented"


def test_prev_review_is_validated() -> None:
    with pytest.raises(InvalidPrevReviewError):
        PrevReview("not-a-sha", None, "approved", "github")
    with pytest.raises(InvalidPrevReviewError):
        PrevReview(SHA_A, "short", "approved", "github")
    with pytest.raises(InvalidPrevReviewError):
        PrevReview(SHA_A, None, "loved", "github")  # type: ignore[arg-type]
    with pytest.raises(InvalidPrevReviewError):
        PrevReview(SHA_A, None, "approved", "email")  # type: ignore[arg-type]
    with pytest.raises(InvalidPrevReviewError):
        FollowUp("maybe")  # type: ignore[arg-type]


def test_line_map_keeps_only_unchanged_lines() -> None:
    old = "a\nb\nc\nd\n"
    new = "x\na\nB\nc\nd\n"
    assert line_map(old, new) == {1: 2, 3: 4, 4: 5}
    assert line_map("", "a\n") == {}


def test_reanchor_moves_ranges_only_when_every_line_is_unchanged() -> None:
    maps: LineMaps = {("f.py", "RIGHT"): {1: 3, 2: 4, 4: 7}, ("f.py", "LEFT"): None}
    single = Draft("f.py", "x", line=2, side="RIGHT", outdated=True)
    assert reanchor(single, maps) == replace(single, line=4, outdated=False)
    ranged = Draft("f.py", "x", line=2, side="RIGHT", start_line=1, start_side="RIGHT")
    assert reanchor(ranged, maps) == replace(ranged, line=4, start_line=3)
    gap = Draft("f.py", "x", line=4, side="RIGHT", start_line=2, start_side="RIGHT")
    assert reanchor(gap, maps).outdated  # line 3 changed inside the range
    spread: LineMaps = {("f.py", "RIGHT"): {1: 1, 2: 5}}
    pushed_apart = Draft("f.py", "x", line=2, side="RIGHT", start_line=1, start_side="RIGHT")
    assert reanchor(pushed_apart, spread).outdated  # lines were inserted inside the range
    split = Draft("f.py", "x", line=7, side="RIGHT", start_line=2, start_side="LEFT")
    assert reanchor(split, maps).outdated  # a range across sides isn't re-anchored
    assert reanchor(Draft("f.py", "x", line=1, side="LEFT"), maps).outdated  # file unreadable
    assert reanchor(Draft("g.py", "x", line=1, side="RIGHT"), maps).outdated  # not mapped
    note = Draft("f.py", "x", outside_diff=True)
    assert reanchor(note, maps) == note
    labelled = Draft("f.py", "x", line=2, outside_diff=True, outdated=True)
    only_right: LineMaps = {("f.py", "RIGHT"): {2: 5}}
    assert reanchor(labelled, only_right) == replace(labelled, line=5, outdated=False)


def test_restrict_diff_leaves_incomplete_diffs_alone() -> None:
    diff = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert [f.path for f in restrict_diff(diff, ["b.py"]).files] == ["b.py"]
    incomplete = replace(diff, complete=False, incomplete_reason="too large")
    assert restrict_diff(incomplete, ["b.py"]) == incomplete


# --- the application's edges --------------------------------------------------------------------


async def test_stored_round_counts_when_github_reviews_cannot_be_read(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    round_id = env.approved_round(repo.head, repo.base)
    env.sync(repo.rebased, repo.rebased_base)
    env.github.error = GitHubRequestError(502, "Bad Gateway")
    stored = env.store.pr(env.ref)
    assert stored is not None
    prev = await env.followups.previous_review(stored)
    assert prev == PrevReview(repo.head, repo.base, "approved", "round", round_id)


async def test_an_unreachable_reviewed_head_counts_as_changed(env: Env) -> None:
    repo = env.repo
    gone = "9" * 40  # never pushed: GitHub's force-push dropped it
    env.github.reviews_of[(REPO, 1)] = [ReviewSummary("me", False, "APPROVED", gone, START, 0)]
    env.sync(repo.rebased, repo.rebased_base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    prev = await env.followups.previous_review(stored)
    assert prev == PrevReview(gone, None, "approved", "github")
    followup = await env.followups.classify(stored)
    assert followup.kind == "rereview" and "couldn't" in followup.note


async def test_refresh_classifies_each_new_head_once_and_isolates_failures(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    env.approved_round(repo.head, repo.base)
    env.sync(repo.rebased, repo.rebased_base)
    other = pull(2, REPO, head=SHA_A, base=SHA_B)
    snapshot = snapshot_of(other, ci_state="passing", reviews=[], automated_reviewer=None,
                           viewer="me")  # fmt: skip
    env.store.apply_sync([snapshot], [], [], env.clock())
    # PR 2's reviews fail: it is skipped (and retried next time), PR 1 still classified.
    real_reviews = env.github.reviews

    async def reviews(repo_name: str, number: int) -> list[ReviewSummary]:
        if number == 2:
            raise GitHubRequestError(500, "boom")
        return await real_reviews(repo_name, number)

    env.github.reviews = reviews  # type: ignore[method-assign,assignment]
    assert await env.followups.refresh() == 1
    assert env.followups.followup(env.ref, repo.rebased) is not None
    assert env.followups.followup(PrRef(REPO, 2), SHA_A) is None
    assert await env.followups.refresh() == 0  # PR 1's head is done; PR 2 fails again

    # A suggested repo's PR is never mirrored, so it is never classified.
    env.store.upsert_repo(RepoConfig("other/repo", suggested=True))
    suggested = snapshot_of(pull(3, "other/repo", head=SHA_A), ci_state="passing", reviews=[],
                            automated_reviewer=None, viewer="me")  # fmt: skip
    env.store.apply_sync([suggested], [], [], env.clock())
    env.github.reviews = real_reviews  # type: ignore[method-assign]
    await env.followups.refresh()
    assert env.followups.followup(PrRef("other/repo", 3), SHA_A) is None


async def test_background_refresh_ticks_after_and_stops_cleanly(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    env.approved_round(repo.head, repo.base)
    env.sync(repo.rebased, repo.rebased_base)
    ticks: list[int] = []

    async def tick() -> None:
        ticks.append(1)

    followups = FollowUps(github=env.github, workspace=env.workspace, store=env.store, after=tick)
    followups.start_refresh()
    followups.start_refresh()  # one at a time: joined, not doubled
    await followups.drain()
    assert followups.followup(env.ref, repo.rebased) is not None
    assert ticks == [1]
    followups.start_refresh()  # nothing left to classify: no tick
    await followups.drain()
    assert ticks == [1]
    await followups.stop()
    await followups.stop()  # nothing running: a no-op
    # A shutdown cancels a running refresh.
    env.sync(repo.conflict, repo.conflict_base)
    followups.start_refresh()
    await followups.stop()


async def test_reanchor_degrades_to_outdated_when_git_fails(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    old = env.store.start_round(env.ref, repo.head, repo.base, "quick", env.clock())
    env.store.save_drafts(old, "", [Draft("app.py", "x", line=10, side="RIGHT")])
    env.sync(repo.pushed, repo.base)
    new = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    await env.workspace.ensure_mirror(REPO)

    async def broken(*args: object, **kwargs: object) -> str:
        raise GitCommandError(("git", "merge-base"), 128, "bad object")

    env.workspace.merge_base = broken  # type: ignore[method-assign]
    with pytest.raises(GitCommandError):
        await env.followups.reanchor(new, repo.pushed, repo.base)
    (draft,) = env.store.drafts(new)
    assert draft.outdated  # never moved by guesswork
    assert env.store.carried_drafts(new)  # still pending: the next preparation tries again


async def test_no_drafts_carry_from_a_posted_or_unsettled_round(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    posted = env.approved_round(repo.head, repo.base)
    conn = env.store._conn
    conn.execute(
        "INSERT INTO drafts (round_id, path, body) VALUES (?, 'app.py', 'posted already')",
        (posted,),
    )
    env.sync(repo.pushed, repo.base)
    after_posted = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    assert env.store.drafts(after_posted) == []
    # A round whose posting may have landed (uncertain) carries nothing either: never twice.
    env.store.save_drafts(after_posted, "", [Draft("app.py", "x", outside_diff=True)])
    attempt = env.store.start_posting(after_posted, "COMMENT", "k", "d", "", [], env.clock())
    env.store.settle_posting(attempt.id, "uncertain", "timeout")
    again = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    assert env.store.drafts(again) == []
    # A retry on the same head carries the drafts as they are (no re-anchoring needed).
    env.store.save_drafts(again, "kept", [Draft("app.py", "y", line=3, side="RIGHT")])
    retry = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    (draft,) = env.store.drafts(retry)
    assert (draft.body, draft.line, draft.outdated) == ("y", 3, False)
    assert env.store.carried_drafts(retry) == []
    # Seeded findings don't repeat a carried draft.
    seeded = Draft("app.py", "y", line=3, side="RIGHT", origin_finding="correctness/minor")
    assert env.store.seed_drafts(retry, [seeded, Draft("app.py", "z", line=4, side="RIGHT")])
    assert [d.body for d in env.store.drafts(retry)] == ["y", "z"]


async def test_frozen_round_refuses_done_and_seeding(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    old = env.store.start_round(env.ref, repo.head, repo.base, "quick", env.clock())
    env.sync(repo.pushed, repo.base)
    env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    with pytest.raises(RoundFrozen):
        env.store.mark_round_done(old)
    assert not env.store.seed_drafts(old, [Draft("app.py", "late finding", outside_diff=True)])
    assert env.store.drafts(old) == []
    reads = ReviewReads(env.store, lambda: SyncStatus(None, False, None))
    view = reads.pr(env.ref)
    assert view.round is not None and view.round.frozen_at is None  # the latest is open


async def test_a_new_head_resets_the_queue_kind_until_classified(env: Env) -> None:
    repo = env.repo
    env.sync(repo.head, repo.base)
    env.approved_round(repo.head, repo.base)
    env.sync(repo.rebased, repo.rebased_base)
    stored = env.store.pr(env.ref)
    assert stored is not None
    await env.followups.classify(stored)
    env.sync(repo.conflict, repo.conflict_base)
    queue = QueueService(env.github, env.store, env.clock)
    (row,) = (await queue.list(QueueFilter())).rows
    assert row.kind == "new" and row.followup_kind is None and row.followup_note == ""


async def test_unreadable_patch_ids_and_files_degrade_to_rereview_and_outdated(env: Env) -> None:
    repo = env.repo
    env.github.reviews_of[(REPO, 1)] = [
        ReviewSummary("me", False, "APPROVED", repo.head, START, 0),
        ReviewSummary("me", False, "COMMENTED", None, START, 0),  # no commit: not a verdict
    ]
    env.sync(repo.rebased, repo.rebased_base)
    stored = env.store.pr(env.ref)
    assert stored is not None

    async def no_ids(*args: object, **kwargs: object) -> dict[str, str]:
        raise GitCommandError(("git", "patch-id"), 1, "boom")

    real_ids = env.workspace.patch_ids
    env.workspace.patch_ids = no_ids  # type: ignore[method-assign]
    followup = await env.followups.classify(stored)
    assert followup.kind == "rereview" and "couldn't" in followup.note
    env.workspace.patch_ids = real_ids  # type: ignore[method-assign]

    # A file gone at the new head, or too large to compare, leaves its drafts outdated.
    env.sync(repo.head, repo.base)
    old = env.store.start_round(env.ref, repo.head, repo.base, "quick", env.clock())
    env.store.save_drafts(
        old,
        "",
        [
            Draft("new.txt", "gone next", line=1, side="RIGHT"),
            Draft("app.py", "too large", line=10, side="RIGHT"),
        ],
    )
    env.sync(repo.pushed, repo.base)
    new = env.store.start_round(env.ref, repo.pushed, repo.base, "quick", env.clock())
    real_show = env.workspace.show_file

    async def show(repo_name: str, sha: str, path: str, *, limit: int) -> bytes | None:
        if path == "app.py":
            raise MirrorFileTooLargeError("app.py is too large")
        return None if sha == repo.pushed else await real_show(repo_name, sha, path, limit=limit)

    env.workspace.show_file = show  # type: ignore[method-assign,assignment]
    assert await env.followups.reanchor(new, repo.pushed, repo.base) == 2
    assert [d.outdated for d in env.store.drafts(new)] == [True, True]
    assert env.store.carried_drafts(new) == []


async def test_a_failed_reanchor_is_a_round_warning_and_rereviews_keep_the_whole_diff(
    conn: sqlite3.Connection,
) -> None:
    store = SqliteReviewStore(conn)
    store.upsert_repo(RepoConfig(REPO, watched=True))
    clock = FakeClock()
    github = FakeGitHub(replace(pull(1), body="", commit_count=1))
    jobs = FakeJobs(store)

    class Broken:
        async def reanchor(self, round_id: int, head_sha: str, merge_base: str) -> int:
            raise GitCommandError(("git", "merge-base"), 128, "bad object")

        def followup(self, ref: PrRef, head_sha: str) -> FollowUp | None:
            return FollowUp("rereview", ["src/app.py"], "New commits since your review", SHA_C)

    pipeline = Pipeline(
        github=github,
        files=FakeFiles(),
        workspace=FakeWorkspace(),
        jobs=jobs,
        store=store,
        bus=EventBus(),
        clock=clock,
        followups=Broken(),
    )
    snapshot = snapshot_of(
        github.pr, ci_state="passing", reviews=[], automated_reviewer=None, viewer="me"
    )
    store.apply_sync([snapshot], [], [], clock() - timedelta(minutes=20))
    result = await pipeline.open(snapshot.ref)
    await pipeline.drain()
    assert result.round_id is not None
    index = jobs.rounds[0].hunk_index
    assert index is not None and "uv.lock" in index.text  # a re-review reads the whole diff
    warnings = store.artifact(result.round_id, "warnings")
    assert isinstance(warnings, list) and any("re-anchored" in w for w in warnings)
    followup = store.artifact(result.round_id, "followup")
    assert isinstance(followup, dict) and followup["kind"] == "rereview"
