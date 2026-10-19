# SPDX-License-Identifier: AGPL-3.0-or-later
"""Posting a review (plan Task 7, spec §7): exactly the previewed drafts, as one atomic GitHub
review against the round's head, with the BLOCKING, newer-head and own-PR rules, and a retry
after a lost response that adopts the review instead of posting it twice.

A real ``review.sqlite``; GitHub is a double (no network).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

import pytest

from jesinia.apps.review.application.posting import PostingFailed, PostingService
from jesinia.apps.review.domain.posting import (
    GITHUB_BODY_LIMIT,
    MARKER_RESERVE,
    CommentOnlyOnOwnPr,
    Draft,
    EarlierReviewPosted,
    EmptyReview,
    InvalidDraftContent,
    InvalidDraftError,
    NeedsConfirmation,
    NoReviewRound,
    PostingInProgress,
    PostingStillUncertain,
    PreviewOutdated,
    ReviewBodyTooLong,
    ReviewedHeadGone,
    commentable_lines,
    compose_review,
    has_blocking,
    marked_body,
    review_warnings,
    seed_drafts,
)
from jesinia.apps.review.domain.queue import PrHeadMovedError, PrRef, RepoConfig, snapshot_of
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.module import REVIEW_DB
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.services.github.application.service import (
    GitHubRequestError,
    GitHubUnavailable,
)
from jesinia.services.github.domain.pr import (
    DraftComment,
    PrCommit,
    PrFile,
    PullRequest,
    ReviewEvent,
    ReviewSummary,
)
from tests.apps.review.fakes import HEAD, FakeClock, pull

NEW_HEAD = "f" * 40
URL = "https://github.com/acme/web/pull/1#pullrequestreview-9"
# GitHub's diff of src/app.py: new lines 1-4 and 11-12 (old 1-3 and 10-11) take comments.
PATCH = "@@ -1,3 +1,4 @@\n a\n-b\n+B\n+c\n d\n@@ -10,2 +11,2 @@ def f():\n x\n-y\n+Y"


def finding(start: int, end: int, text: str, *, path: str = "src/app.py") -> dict[str, object]:
    return {
        "group": "correctness",
        "severity": "important",
        "path": path,
        "start_line": start,
        "end_line": end,
        "explanation": text,
        "rule": {"id": "lang-01_python_standards.md › Errors", "layer": "base"},
    }


# --- doubles ------------------------------------------------------------------------------------


@dataclass
class FakeGitHub:
    """GitHub as posting sees it: the live PR, its commits, its reviews and the one write."""

    pr: PullRequest
    commits: list[str] = field(default_factory=lambda: [HEAD])
    commits_truncated: bool = False
    reviews_on_pr: list[ReviewSummary] = field(default_factory=list)
    posted: list[dict[str, object]] = field(default_factory=list)
    # Raised by create_review; ``lose_response`` raises it *after* GitHub took the review.
    error: Exception | None = None
    lose_response: bool = False
    viewer_calls: int = 0
    clock: FakeClock = field(default_factory=FakeClock)
    get_error: Exception | None = None

    async def get(self, repo: str, number: int) -> PullRequest:
        if self.get_error is not None:
            raise self.get_error
        return self.pr

    async def commits_of(self, pr: PullRequest) -> tuple[PullRequest, list[PrCommit]]:
        listed = [PrCommit(sha, "m", "alice", None) for sha in self.commits]
        return replace(pr, commits_truncated=self.commits_truncated), listed

    async def reviews(self, repo: str, number: int) -> list[ReviewSummary]:
        return list(self.reviews_on_pr)

    async def viewer(self) -> str:
        self.viewer_calls += 1
        return "me"

    async def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_sha: str,
        event: ReviewEvent,
        body: str,
        comments: Sequence[DraftComment],
    ) -> str:
        if self.error is not None and not self.lose_response:
            raise self.error
        self.posted.append(
            {"commit_sha": commit_sha, "event": event, "body": body, "comments": list(comments)}
        )
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
            event, "COMMENTED"
        )
        self.reviews_on_pr.append(
            ReviewSummary("Me", False, state, commit_sha, self.clock(), len(comments), body, URL)
        )
        if self.error is not None:
            raise self.error
        return URL


class FileSource:
    def __init__(self) -> None:
        self.result: list[PrFile] = []
        self.error: Exception | None = None
        self.calls = 0

    async def files(self, repo: str, number: int, head_sha: str) -> list[PrFile]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.result)


@dataclass
class Env:
    store: SqliteReviewStore
    github: FakeGitHub
    source: FileSource
    clock: FakeClock
    service: PostingService
    ref: PrRef
    round_id: int

    async def post(
        self, event: ReviewEvent, confirm: Sequence[str] = (), *, key: str = "k1"
    ) -> str:
        preview = await self.service.preview(self.round_id, event)
        return await self.service.post(
            self.round_id, event, confirm, key=key, digest=preview.digest
        )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path, REVIEW_DB)
    yield connection
    connection.close()


def _env(conn: sqlite3.Connection, findings: list[dict[str, object]]) -> Env:
    store = SqliteReviewStore(conn)
    store.upsert_repo(RepoConfig("acme/web", watched=True))
    store.set_viewer("me")
    clock = FakeClock()
    pr = pull(1)
    snapshot = snapshot_of(pr, ci_state="passing", reviews=[], automated_reviewer=None, viewer="me")
    store.apply_sync([snapshot], [], [], clock())
    ref = snapshot.ref
    round_id = store.start_round(ref, HEAD, pr.base_sha, "quick", clock())
    store.put_artifact(round_id, "findings", {"findings": findings})
    store.finish_round(round_id, clock(), None, "approve")
    # GitHub needs a body to comment: the author's summary.
    conn.execute("UPDATE rounds SET summary = 'Review notes.' WHERE id = ?", (round_id,))
    store.put_pr_files(ref, HEAD, [PrFile("src/app.py", "modified", 3, 2, PATCH, None)])
    github = FakeGitHub(pr, clock=clock)
    source = FileSource()
    service = PostingService(github=github, writer=github, files=source, store=store, clock=clock)
    return Env(store, github, source, clock, service, ref, round_id)


@pytest.fixture
def env(conn: sqlite3.Connection) -> Env:
    return _env(conn, [finding(2, 3, "The error is swallowed.")])


# --- the brief's tests --------------------------------------------------------------------------


async def test_blocking_comment_warns_on_approve(env: Env) -> None:
    drafts = await env.service.drafts(env.round_id)
    edited = [replace(drafts.drafts[0], body="This is bLoCkInG: fix it first.")]
    await env.service.save_drafts(env.round_id, "Looks good otherwise.", edited)

    approve = await env.service.preview(env.round_id, "APPROVE")
    assert approve.warnings == ("blocking_comment",)
    assert (await env.service.preview(env.round_id, "COMMENT")).warnings == ()
    with pytest.raises(NeedsConfirmation) as refused:
        await env.post("APPROVE")
    assert refused.value.warnings == ("blocking_comment",)
    assert env.github.posted == []

    assert await env.post("APPROVE", ["blocking_comment"]) == URL
    assert len(env.github.posted) == 1 and env.github.posted[0]["event"] == "APPROVE"

    # The final body counts too, in any case.
    await env.service.save_drafts(env.round_id, "blocking: the migration", [])
    assert (await env.service.preview(env.round_id, "APPROVE")).warnings == ("blocking_comment",)


async def test_newer_head_requires_confirmation(env: Env) -> None:
    env.github.pr = replace(env.github.pr, head_sha=NEW_HEAD)
    env.github.commits = [HEAD, NEW_HEAD]
    preview = await env.service.preview(env.round_id, "APPROVE")
    assert preview.warnings == ("newer_head",) and preview.head_sha == HEAD
    with pytest.raises(NeedsConfirmation) as refused:
        await env.post("APPROVE")
    assert refused.value.warnings == ("newer_head",)
    assert await env.post("APPROVE", ["newer_head"]) == URL
    # Posting targets the commit the round reviewed, not the new head.
    assert env.github.posted[0]["commit_sha"] == HEAD

    # A head GitHub can't list fully may still contain the round's commit: it warns.
    env.github.commits, env.github.commits_truncated = [NEW_HEAD], True
    assert (await env.service.preview(env.round_id, "COMMENT")).warnings == ("newer_head",)
    # Force-pushed away: the reviewed commit is no longer on the PR at all.
    env.github.commits_truncated = False
    with pytest.raises(ReviewedHeadGone):
        await env.service.preview(env.round_id, "COMMENT")


async def test_own_pr_offers_comment_only(env: Env) -> None:
    env.github.pr = replace(env.github.pr, author="Me")
    preview = await env.service.preview(env.round_id, "APPROVE")
    assert preview.allowed_events == ("COMMENT",)
    assert preview.warnings == ("own_pr_comment_only",)
    with pytest.raises(CommentOnlyOnOwnPr):
        await env.post("APPROVE", ["own_pr_comment_only"])
    with pytest.raises(CommentOnlyOnOwnPr):
        await env.post("REQUEST_CHANGES", ["own_pr_comment_only"])
    assert env.github.posted == []
    comment = await env.service.preview(env.round_id, "COMMENT")
    assert comment.warnings == () and comment.allowed_events == ("COMMENT",)
    assert await env.post("COMMENT") == URL
    other = replace(env.github.pr, author="alice")
    env.github.pr = other
    assert (await env.service.preview(env.round_id, "COMMENT")).allowed_events == (
        "APPROVE",
        "REQUEST_CHANGES",
        "COMMENT",
    )


async def test_post_is_one_atomic_review(env: Env) -> None:
    drafts = [
        Draft("src/app.py", "First.", line=2, side="RIGHT"),
        Draft("src/app.py", "Second.", line=11, side="LEFT"),
    ]
    await env.service.drafts(env.round_id)  # the client reads (and sees the seeds) first
    await env.service.save_drafts(env.round_id, "Summary.", drafts)
    preview = await env.service.preview(env.round_id, "REQUEST_CHANGES")
    assert preview.body == "Summary."
    assert [c.body for c in preview.comments] == ["First.", "Second."]

    # A refusal posts nothing at all, keeps the drafts, and keeps GitHub's cause.
    env.github.error = GitHubRequestError(
        422, "Unprocessable Entity: line must be part of the diff"
    )
    with pytest.raises(PostingFailed) as failed:
        await env.post("REQUEST_CHANGES")
    assert "line must be part of the diff" in str(failed.value)
    assert not failed.value.maybe_posted
    assert isinstance(failed.value.__cause__, GitHubRequestError)
    assert env.github.posted == []
    assert len((await env.service.drafts(env.round_id)).drafts) == 2
    stored = env.store.round(env.round_id)
    assert stored is not None and stored.posted_review_url is None

    env.github.error = None
    assert await env.post("REQUEST_CHANGES", key="k2") == URL
    assert len(env.github.posted) == 1
    sent = env.github.posted[0]
    attempt = env.store.postings(env.round_id)[-1]
    assert sent["body"] == f"{preview.body}\n\n<!-- jesinia-posting:{attempt.id} -->"
    assert sent["comments"] == list(preview.comments)
    assert "invisible marker" in " ".join(preview.notes)
    stored = env.store.round(env.round_id)
    assert stored is not None and stored.posted_review_url == URL and stored.done
    # The same key again is the same posting: nothing new is sent.
    assert await env.post("REQUEST_CHANGES", key="k2") == URL
    assert len(env.github.posted) == 1


async def test_outside_diff_findings_go_in_the_body_and_the_review_posts(
    conn: sqlite3.Connection,
) -> None:
    env = _env(conn, [finding(2, 3, "Inside the diff."), finding(40, 41, "Far below the diff.")])
    shown = await env.service.drafts(env.round_id)
    assert not shown.files_pending
    assert [("Inside the diff." in d.body, d.outside_diff) for d in shown.drafts] == [
        (True, False),
        (False, False),  # only the author marks a draft outside the diff
    ]
    assert shown.in_body == (False, True)  # where it posts, worked out on each read
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert len(preview.comments) == 1 and preview.comments[0].line == 3
    assert "Far below the diff." in preview.body and "src/app.py" in preview.body
    assert "lines 40" in preview.body

    assert await env.post("COMMENT") == URL
    sent = env.github.posted[0]
    comments = sent["comments"]
    assert isinstance(comments, list) and len(comments) == 1
    assert all("Far below" not in c.body for c in comments)
    assert "Far below the diff." in str(sent["body"])


async def test_multi_line_draft_round_trips_its_start_line_and_side(env: Env) -> None:
    ranged = Draft(
        "src/app.py", "Across lines.", line=4, side="RIGHT", start_line=2, start_side="RIGHT"
    )
    await env.service.drafts(env.round_id)  # the client reads (and sees the seeds) first
    await env.service.save_drafts(env.round_id, "Ranged.", [ranged])
    stored = (await env.service.drafts(env.round_id)).drafts
    assert len(stored) == 1 and stored[0].id is not None
    assert (stored[0].start_line, stored[0].start_side, stored[0].line, stored[0].side) == (
        2,
        "RIGHT",
        4,
        "RIGHT",
    )
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert preview.comments == (
        DraftComment("src/app.py", 4, "RIGHT", "Across lines.", 2, "RIGHT"),
    )
    await env.post("COMMENT")
    assert env.github.posted[0]["comments"] == list(preview.comments)


async def test_retry_after_lost_response_does_not_post_twice(env: Env) -> None:
    env.github.error = GitHubRequestError(None, "ReadTimeout")
    env.github.lose_response = True  # GitHub took the review; the answer never came back
    with pytest.raises(PostingFailed) as lost:
        await env.post("COMMENT", key="k1")
    assert lost.value.maybe_posted
    assert len(env.github.posted) == 1

    env.github.error, env.github.lose_response = None, False
    env.clock.advance(1)
    # The retry (same key, or a new one) adopts the review GitHub already has.
    assert await env.post("COMMENT", key="k1") == URL
    assert await env.post("COMMENT", key="k-new") == URL
    assert len(env.github.posted) == 1
    stored = env.store.round(env.round_id)
    assert stored is not None and stored.posted_review_url == URL


# --- more of posting ----------------------------------------------------------------------------


async def test_a_lost_response_with_no_review_on_github_posts_again(env: Env) -> None:
    env.github.error = GitHubRequestError(None, "ConnectError")  # never reached GitHub
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error = None
    # Someone else's review, an older one of mine and one with another body don't count.
    stale = env.clock() - timedelta(hours=1)
    env.github.reviews_on_pr = [
        ReviewSummary("bob", False, "COMMENTED", HEAD, env.clock(), 0, "", "u1"),
        ReviewSummary("me", False, "COMMENTED", HEAD, stale, 0, "", "u2"),
        ReviewSummary("me", False, "COMMENTED", HEAD, env.clock(), 0, "other", "u3"),
        ReviewSummary("me", False, "APPROVED", HEAD, env.clock(), 0, "", "u4"),
        ReviewSummary("me", False, "COMMENTED", NEW_HEAD, env.clock(), 0, "", "u5"),
        ReviewSummary("me", False, "COMMENTED", HEAD, None, 0, "", "u6"),
    ]
    env.clock.advance(4)  # past the grace period: GitHub has had time to list it
    assert await env.post("COMMENT") == URL
    assert len(env.github.posted) == 1


async def test_a_definite_refusal_is_not_adopted_later(env: Env) -> None:
    env.github.error = GitHubUnavailable("no GitHub token")
    with pytest.raises(PostingFailed) as failed:
        await env.post("COMMENT")
    assert not failed.value.maybe_posted
    env.github.error = None
    assert await env.post("COMMENT") == URL
    assert len(env.github.posted) == 1


async def test_a_stale_preview_is_refused(env: Env) -> None:
    preview = await env.service.preview(env.round_id, "COMMENT")
    await env.service.save_drafts(env.round_id, "Changed after the preview.", [])
    with pytest.raises(PreviewOutdated):
        await env.service.post(env.round_id, "COMMENT", [], key="k", digest=preview.digest)
    approve = await env.service.preview(env.round_id, "APPROVE")
    with pytest.raises(PreviewOutdated):  # a digest for another event
        await env.service.post(env.round_id, "COMMENT", [], key="k", digest=approve.digest)
    assert env.github.posted == []


async def test_a_comment_needs_a_body_or_comments(env: Env) -> None:
    await env.service.drafts(env.round_id)  # the client reads (and sees the seeds) first
    await env.service.save_drafts(env.round_id, "   ", [])
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert "needs a summary or an inline comment" in " ".join(preview.notes)
    for event in ("COMMENT", "REQUEST_CHANGES"):
        with pytest.raises(EmptyReview):
            await env.post(event)
    assert await env.post("APPROVE") == URL  # an approval may be bare


async def test_an_inline_only_request_changes_posts(env: Env) -> None:
    only = [Draft("src/app.py", "Fix this line.", line=3, side="RIGHT")]
    await env.service.drafts(env.round_id)  # the client reads (and sees the seeds) first
    await env.service.save_drafts(env.round_id, "", only)
    preview = await env.service.preview(env.round_id, "REQUEST_CHANGES")
    assert (
        preview.body == ""
        and len(preview.comments) == 1
        and not any("needs a summary" in note for note in preview.notes)
    )
    assert await env.post("REQUEST_CHANGES") == URL
    sent = env.github.posted[0]
    assert str(sent["body"]).startswith("<!-- jesinia-posting:")  # the marker alone


async def test_a_draft_outside_githubs_diff_moves_to_the_body(env: Env) -> None:
    await env.service.drafts(env.round_id)  # the client reads (and sees the seeds) first
    await env.service.save_drafts(
        env.round_id,
        "",
        [
            Draft("src/app.py", "Edited onto line 30.", line=30, side="RIGHT"),
            Draft("src/app.py", "Spans two hunks.", line=11, side="RIGHT", start_line=2,
                  start_side="RIGHT"),
            Draft("other.py", "No such file.", line=1, side="RIGHT"),
            Draft("src/app.py", "General note.", outside_diff=True),
        ],
    )  # fmt: skip
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert preview.comments == ()
    for text in ("Edited onto line 30.", "Spans two hunks.", "No such file.", "General note."):
        assert text in preview.body


async def test_drafts_never_read_github_and_a_missing_file_list_is_not_stored(env: Env) -> None:
    conn = env.store._conn
    conn.execute("DELETE FROM pr_files")
    env.source.error = AssertionError("drafts must not ask GitHub")
    shown = await env.service.drafts(env.round_id)
    assert shown.files_pending and shown.in_body is None and env.source.calls == 0
    assert [d.outside_diff for d in shown.drafts] == [False]  # seeded inline regardless
    # GitHub is back: the preview reads the list and the finding anchors inline.
    env.source.error = None
    env.source.result = [PrFile("src/app.py", "modified", 3, 2, PATCH, None)]
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert len(preview.comments) == 1


async def test_files_missing_from_the_cache_are_read_once_or_degrade_visibly(env: Env) -> None:
    conn = env.store._conn
    conn.execute("DELETE FROM pr_files")
    env.source.result = [PrFile("src/app.py", "modified", 3, 2, PATCH, None)]
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert len(preview.comments) == 1 and env.source.calls == 1
    assert not any("couldn't be read" in note for note in preview.notes)

    conn.execute("DELETE FROM pr_files")
    env.source.error = PrHeadMovedError("acme/web#1 is now at ffff")
    moved = await env.service.preview(env.round_id, "COMMENT")
    assert moved.comments == ()
    assert "GitHub's list of this head's files couldn't be read" in " ".join(moved.notes)
    assert "The error is swallowed." in moved.body


async def test_the_viewer_comes_from_github_when_never_synced(env: Env) -> None:
    env.store._conn.execute("DELETE FROM settings WHERE key = 'viewer'")
    await env.service.preview(env.round_id, "COMMENT")
    assert env.github.viewer_calls == 1


async def test_rounds_are_found_by_pr_and_refused_when_missing(env: Env) -> None:
    assert await env.service.round_for(env.ref) == env.round_id
    later = env.store.start_round(env.ref, HEAD, "b" * 40, "quick", env.clock())
    assert await env.service.round_for(env.ref) == later
    with pytest.raises(NoReviewRound):
        await env.service.round_for(PrRef("acme/web", 99))
    with pytest.raises(NoReviewRound):
        await env.service.preview(12345, "COMMENT")
    with pytest.raises(NoReviewRound):
        await env.service.drafts(12345)


# --- the domain rules ---------------------------------------------------------------------------


def test_blocking_is_found_case_insensitively_anywhere() -> None:
    assert has_blocking("x", [DraftComment("a", 1, "RIGHT", "Blocking!")])
    assert has_blocking("NON-BLOCKING nit", [])
    assert not has_blocking("block ing", [DraftComment("a", 1, "RIGHT", "fine")])


def test_warnings_follow_the_event() -> None:
    review = compose_review("BLOCKING", [], commentable_lines([]))
    assert review_warnings("APPROVE", review, own_pr=False, newer_head=True) == (
        "blocking_comment",
        "newer_head",
    )
    assert review_warnings("REQUEST_CHANGES", review, own_pr=True, newer_head=False) == (
        "own_pr_comment_only",
    )
    assert review_warnings("COMMENT", review, own_pr=True, newer_head=False) == ()


def test_commentable_lines_come_from_githubs_patches() -> None:
    lines = commentable_lines(
        [
            PrFile("src/app.py", "modified", 3, 2, PATCH, None),
            PrFile("big.bin", "added", 0, 0, None, None),
            PrFile("odd.py", "modified", 1, 1, "not a patch", None),
        ]
    )
    assert lines.anchors(Draft("src/app.py", "x", line=4, side="RIGHT"))
    assert lines.anchors(Draft("src/app.py", "x", line=10, side="LEFT"))
    assert not lines.anchors(Draft("src/app.py", "x", line=5, side="RIGHT"))
    assert not lines.anchors(Draft("big.bin", "x", line=1, side="RIGHT"))
    assert not lines.anchors(Draft("odd.py", "x", line=1, side="RIGHT"))
    assert not lines.anchors(Draft("src/app.py", "x", outside_diff=True))


def test_drafts_are_validated_like_githubs_comments() -> None:
    cases: list[Callable[[], Draft]] = [
        lambda: Draft("", "x", line=1, side="RIGHT"),
        lambda: Draft("a.py", " ", line=1, side="RIGHT"),
        lambda: Draft("a.py", "x"),  # inline without a line
        lambda: Draft("a.py", "x", line=3, side="RIGHT", start_line=3, start_side="RIGHT"),
        lambda: Draft("a.py", "x", line=3, side="RIGHT", start_line=1),
        lambda: Draft("a.py", "x" * 70_000, line=1, side="RIGHT"),
        lambda: Draft("a.py", "x", line=1, side="UP"),  # type: ignore[arg-type]
        lambda: Draft("a.py", "x", outside_diff=True, line=0),
    ]
    for bad in cases:
        with pytest.raises(InvalidDraftError):
            bad()
    assert Draft("a.py", "x", outside_diff=True).line is None


def test_seed_drafts_take_well_formed_findings_only() -> None:
    seeded = seed_drafts(
        [
            finding(3, 3, "One line."),
            finding(2, 4, "A range."),
            {"group": "style", "severity": "minor", "path": "src/app.py", "start_line": 12,
             "end_line": 12, "explanation": "Name it.", "rule": None},
            {"severity": "bogus"},
            "not a finding",
            finding(5, 2, "Backwards."),
            finding(40, 40, "Outside GitHub's diff, still inline until placed."),
        ],
    )  # fmt: skip
    assert [(d.line, d.start_line, d.outside_diff) for d in seeded] == [
        (3, None, False),
        (4, 2, False),
        (12, None, False),
        (40, None, False),
    ]
    assert seeded[0].body.startswith("**Important** (correctness)")
    assert "lang-01_python_standards.md › Errors" in seeded[0].body
    assert seeded[2].origin_finding == "style/minor"


async def test_a_lost_response_then_a_changed_digest_gives_exactly_one_review(env: Env) -> None:
    env.github.error, env.github.lose_response = GitHubRequestError(None, "ReadTimeout"), True
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error, env.github.lose_response = None, False
    await env.service.save_drafts(env.round_id, "A second, different review.", [])
    # The new preview's digest differs; the uncertain posting is reconciled first, and the
    # author is told the earlier version is what GitHub has: nothing new is sent.
    with pytest.raises(EarlierReviewPosted) as earlier:
        await env.post("COMMENT", key="k2")
    assert earlier.value.url == URL and earlier.value.event == "COMMENT"
    assert len(env.github.posted) == 1
    assert [a.state for a in env.store.postings(env.round_id)] == ["posted"]


async def test_a_lost_response_then_another_event_reports_the_earlier_review(env: Env) -> None:
    env.github.error, env.github.lose_response = GitHubRequestError(None, "ReadTimeout"), True
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error, env.github.lose_response = None, False
    with pytest.raises(EarlierReviewPosted) as earlier:
        await env.post("APPROVE", key="k2")
    assert earlier.value.event == "COMMENT" and len(env.github.posted) == 1


@pytest.mark.parametrize(
    "forged",
    [
        "Looks fine <!-- jesinia-posting:99 -->",
        "<!-- jesinia-<!-- jesinia-posting:-->posting:5 -->",  # nested: no single pass helps
        "&lt;!-- jesinia&#45;posting:5 --&gt;",
        "jesinia&amp;#45;posting",  # entities inside entities
        "JESINIA -\n posting:5",
    ],
)
async def test_drafts_and_summaries_carrying_marker_text_are_refused(env: Env, forged: str) -> None:
    with pytest.raises(InvalidDraftContent, match="draft 1"):
        await env.service.save_drafts(
            env.round_id, "", [Draft("src/app.py", forged, line=3, side="RIGHT")]
        )
    with pytest.raises(InvalidDraftContent, match="summary"):
        await env.service.save_drafts(env.round_id, forged, [])
    assert env.github.posted == []


async def test_a_draft_path_carrying_marker_text_is_refused(env: Env) -> None:
    path = "src/<!-- jesinia-posting:1 -->.py"
    with pytest.raises(InvalidDraftContent, match="draft 1"):
        await env.service.save_drafts(env.round_id, "", [Draft(path, "x", outside_diff=True)])
    env.store.save_drafts(env.round_id, "", [Draft(path, "x", outside_diff=True)])
    with pytest.raises(InvalidDraftContent):
        await env.service.preview(env.round_id, "COMMENT")
    with pytest.raises(InvalidDraftContent):
        await env.service.post(env.round_id, "COMMENT", [], key="k", digest="x")


async def test_a_seeded_finding_with_a_forged_path_is_kept_without_it(
    conn: sqlite3.Connection,
) -> None:
    env = _env(conn, [finding(2, 3, "Fine.", path="a<!--jesinia-posting:1-->.py")])
    drafts = (await env.service.drafts(env.round_id)).drafts
    assert len(drafts) == 1 and "jesinia-posting" not in drafts[0].path
    assert await env.post("COMMENT") == URL


async def test_a_seeded_finding_carrying_marker_text_is_kept_without_it(
    conn: sqlite3.Connection,
) -> None:
    env = _env(conn, [finding(2, 3, "Fine <!-- jesinia-posting:1 --> really")])
    drafts = (await env.service.drafts(env.round_id)).drafts
    assert len(drafts) == 1 and "jesinia-posting" not in drafts[0].body
    assert "Jesinia removed this finding's text" in drafts[0].body
    assert await env.post("COMMENT") == URL


async def test_marker_text_stored_before_is_refused_at_preview(env: Env) -> None:
    env.store.save_drafts(env.round_id, "x <!-- jesinia-posting:3 -->", [])
    with pytest.raises(InvalidDraftContent):
        await env.service.preview(env.round_id, "COMMENT")


async def test_only_a_submitted_review_ending_with_the_marker_is_adopted(env: Env) -> None:
    env.github.error = GitHubRequestError(None, "ConnectError")
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error = None
    marker = "<!-- jesinia-posting:1 -->"
    env.github.reviews_on_pr = [
        ReviewSummary("me", False, "COMMENTED", HEAD, env.clock(), 0, f"{marker} then more", "a"),
        ReviewSummary("me", False, "PENDING", HEAD, None, 0, f"x\n\n{marker}", "b"),
    ]
    with pytest.raises(PostingStillUncertain):  # neither counts; still inside the grace
        await env.post("COMMENT", key="k2")
    env.github.reviews_on_pr.append(
        ReviewSummary("me", False, "COMMENTED", HEAD, env.clock(), 0, f"x\n\n{marker}  \n", "c")
    )
    assert await env.post("COMMENT", key="k3") == "c"
    assert env.github.posted == []


async def test_a_retry_inside_the_grace_period_is_409_and_posts_nothing(env: Env) -> None:
    env.github.error = GitHubRequestError(504, "Gateway Timeout")
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error = None
    env.clock.advance(1)
    with pytest.raises(PostingStillUncertain) as wait:
        await env.post("COMMENT", key="k2")
    assert wait.value.retry_after == 120
    assert env.github.posted == []
    assert [a.state for a in env.store.postings(env.round_id)] == ["uncertain"]
    env.clock.advance(3)  # past the grace period, and still no marked review
    assert await env.post("COMMENT", key="k3") == URL
    assert len(env.github.posted) == 1
    assert [a.state for a in env.store.postings(env.round_id)] == ["not_posted", "posted"]


async def test_a_failing_review_listing_leaves_the_posting_uncertain(env: Env) -> None:
    env.github.error = GitHubRequestError(None, "ReadTimeout")
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error = None
    env.clock.advance(10)

    async def down(repo: str, number: int) -> list[ReviewSummary]:
        raise GitHubRequestError(503, "Service Unavailable")

    env.github.reviews = down  # type: ignore[method-assign]
    with pytest.raises(GitHubRequestError):
        await env.post("COMMENT", key="k2")
    assert env.github.posted == []
    assert [a.state for a in env.store.postings(env.round_id)] == ["uncertain"]


async def test_a_started_row_left_by_a_crash_is_reconciled_as_uncertain(env: Env) -> None:
    left = env.store.start_posting(env.round_id, "COMMENT", "crashed", "d", "b", [], env.clock())
    with pytest.raises(PostingStillUncertain):
        await env.post("COMMENT")
    marked = f"Review notes.\n\n<!-- jesinia-posting:{left.id} -->"
    env.github.reviews_on_pr = [
        ReviewSummary("me", False, "COMMENTED", HEAD, env.clock(), 0, marked, "landed")
    ]
    with pytest.raises(EarlierReviewPosted) as earlier:  # its digest "d" isn't this one
        await env.post("COMMENT")
    assert earlier.value.url == "landed" and env.github.posted == []


async def test_losing_the_in_flight_race_is_a_typed_conflict(env: Env) -> None:
    store = env.store
    original = store.start_posting

    def racing(*args: object, **kwargs: object) -> object:
        original(env.round_id, "COMMENT", "other-process", "d", "b", [], env.clock())
        return original(*args, **kwargs)  # type: ignore[arg-type]

    store.start_posting = racing  # type: ignore[method-assign,assignment]
    with pytest.raises(PostingInProgress) as busy:
        await env.post("COMMENT")
    assert busy.value.retry_after == 5
    assert env.github.posted == []


async def test_a_5xx_on_create_review_is_uncertain_and_the_retry_adopts(env: Env) -> None:
    env.github.error, env.github.lose_response = GitHubRequestError(502, "Bad Gateway"), True
    with pytest.raises(PostingFailed) as lost:
        await env.post("COMMENT")
    assert lost.value.maybe_posted
    assert [a.state for a in env.store.postings(env.round_id)] == ["uncertain"]
    env.github.error, env.github.lose_response = None, False
    assert await env.post("COMMENT", key="k2") == URL
    assert len(env.github.posted) == 1


async def test_adoption_matches_only_jesinias_marker(env: Env) -> None:
    preview = await env.service.preview(env.round_id, "COMMENT")
    env.github.error = GitHubRequestError(None, "ConnectError")  # never reached GitHub
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error = None
    # A manual review with the very same body, and a bare approval, by the viewer.
    env.github.reviews_on_pr = [
        ReviewSummary("me", False, "COMMENTED", HEAD, env.clock(), 0, preview.body, "u1"),
        ReviewSummary("me", False, "APPROVED", HEAD, env.clock(), 0, "", "u2"),
        ReviewSummary("bob", False, "COMMENTED", HEAD, env.clock(), 0,
                      "<!-- jesinia-posting:1 -->", "u3"),
        ReviewSummary("me", False, "COMMENTED", NEW_HEAD, env.clock(), 0,
                      "<!-- jesinia-posting:1 -->", "u4"),
    ]  # fmt: skip
    env.clock.advance(4)
    assert await env.post("COMMENT", key="k2") == URL  # posted, not adopted
    assert len(env.github.posted) == 1
    assert [a.state for a in env.store.postings(env.round_id)] == ["not_posted", "posted"]


async def test_a_marked_review_listed_without_a_url_is_adopted_with_the_prs(env: Env) -> None:
    env.github.error, env.github.lose_response = GitHubRequestError(None, "ReadTimeout"), True
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error, env.github.lose_response = None, False
    env.github.reviews_on_pr = [replace(r, url=None) for r in env.github.reviews_on_pr]
    assert await env.post("COMMENT") == "https://github.com/acme/web/pull/1"
    assert len(env.github.posted) == 1


def test_only_one_posting_per_round_is_in_flight(env: Env) -> None:
    env.store.start_posting(env.round_id, "COMMENT", "a", "d", "b", [], env.clock())
    with pytest.raises(sqlite3.IntegrityError):
        env.store.start_posting(env.round_id, "COMMENT", "b", "d", "b", [], env.clock())


async def test_a_reused_key_of_a_posted_attempt_is_never_sent_again(env: Env) -> None:
    assert await env.post("COMMENT", key="same") == URL
    await env.service.save_drafts(env.round_id, "Different now.", [])
    assert await env.post("COMMENT", key="same") == URL
    assert len(env.github.posted) == 1


async def test_drafts_are_seeded_once_from_both_passes_without_repeats(
    conn: sqlite3.Connection,
) -> None:
    env = _env(conn, [finding(2, 3, "Swallowed."), finding(11, 11, "", path="")])
    env.store.put_artifact(env.round_id, "correctness", {"findings": [finding(2, 3, "Swallowed.")]})
    drafts = (await env.service.drafts(env.round_id)).drafts
    assert len(drafts) == 1
    await env.service.save_drafts(env.round_id, "", [])
    assert (await env.service.drafts(env.round_id)).drafts == ()  # a deletion sticks


async def test_saving_drafts_is_bounded(env: Env) -> None:
    many = [Draft("a.py", "x", outside_diff=True)] * 501
    with pytest.raises(InvalidDraftError):
        await env.service.save_drafts(env.round_id, "", many)
    with pytest.raises(InvalidDraftError):
        await env.service.save_drafts(env.round_id, "x" * 70_000, [])
    with pytest.raises(InvalidDraftError):
        Draft("a.py", "x", outside_diff=True).comment()


# --- PR 2 fix wave: settling unsettled postings in the background (M3) -------------------------


async def test_a_lost_response_is_settled_in_the_background(env: Env) -> None:
    """M3: an ``uncertain`` attempt whose review is on GitHub is adopted by the job (never
    by a GET): the round shows posted without the author posting again."""
    env.github.error = GitHubRequestError(None, "ReadTimeout")
    env.github.lose_response = True
    with pytest.raises(PostingFailed):
        await env.post("COMMENT")
    env.github.error, env.github.lose_response = None, False
    (attempt,) = env.store.postings(env.round_id)
    assert attempt.state == "uncertain"

    assert await env.service.reconcile_unsettled() == 1
    stored = env.store.round(env.round_id)
    assert stored is not None and stored.done and stored.posted_review_url == URL
    assert len(env.github.posted) == 1
    assert await env.service.reconcile_unsettled() == 0  # nothing left


async def test_a_crash_left_attempt_never_on_github_becomes_not_posted(env: Env) -> None:
    preview = await env.service.preview(env.round_id, "COMMENT")
    env.store.start_posting(
        env.round_id, "COMMENT", "k1", preview.digest, preview.body, [], env.clock()
    )  # then the server died before calling GitHub
    assert await env.service.reconcile_unsettled() == 0  # within the grace (R49): left alone
    assert env.store.postings(env.round_id)[0].state == "started"
    env.clock.advance(4)
    assert await env.service.reconcile_unsettled() == 1
    (attempt,) = env.store.postings(env.round_id)
    assert attempt.state == "not_posted"
    stored = env.store.round(env.round_id)
    assert stored is not None and not stored.done
    assert await env.post("COMMENT") == URL  # the author can post it now


async def test_background_reconcile_survives_github_failures(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    preview = await env.service.preview(env.round_id, "COMMENT")
    env.store.start_posting(
        env.round_id, "COMMENT", "k1", preview.digest, preview.body, [], env.clock()
    )
    env.clock.advance(4)

    async def failing(repo: str, number: int) -> list[ReviewSummary]:
        raise GitHubRequestError(502, "Bad Gateway")

    env.github.reviews = failing  # type: ignore[method-assign]
    assert await env.service.reconcile_unsettled() == 0
    (attempt,) = env.store.postings(env.round_id)
    assert attempt.state == "started"  # kept: nothing is posted until it can be settled
    assert "Bad Gateway" in caplog.text


async def test_one_unexpected_reconcile_error_does_not_stop_the_others(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    # Recorded before the next round starts: a replaced (frozen) round takes no posting.
    env.store.start_posting(env.round_id, "COMMENT", "k1", "d", "body", [], env.clock())
    other = env.store.start_round(env.ref, HEAD, "b" * 40, "quick", env.clock())
    env.store.start_posting(other, "COMMENT", "k1", "d", "body", [], env.clock())
    env.clock.advance(4)
    real = env.github.reviews
    calls = 0

    async def flaky(repo: str, number: int) -> list[ReviewSummary]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the response was not JSON")
        return await real(repo, number)

    env.github.reviews = flaky  # type: ignore[method-assign]
    assert await env.service.reconcile_unsettled() == 1
    first, second = env.store.postings(env.round_id)[0], env.store.postings(other)[0]
    assert first.state == "started" and second.state == "not_posted"
    assert "the response was not JSON" in caplog.text


# --- Codex round 1: the composed body against GitHub's limit -----------------------------------


def _outside(path: str, body: str) -> Draft:
    return Draft(path=path, body=body, outside_diff=True)


async def test_a_composed_body_over_githubs_limit_is_refused_at_preview_and_post(
    env: Env,
) -> None:
    """Outside-diff drafts, their labels and the summary go into one body: over GitHub's
    65,536 characters (marker included) it is refused before anything is sent."""
    await env.service.save_drafts(
        env.round_id, "Review notes.", [_outside("d/" + "p" * 1022, "x" * 65_000)]
    )
    with pytest.raises(ReviewBodyTooLong) as refused:
        await env.service.preview(env.round_id, "COMMENT")
    assert refused.value.limit == GITHUB_BODY_LIMIT == 65_536
    assert refused.value.length > GITHUB_BODY_LIMIT
    assert f"{refused.value.length:,} characters" in str(refused.value)
    assert "GitHub allows 65,536" in str(refused.value)
    with pytest.raises(ReviewBodyTooLong):
        await env.service.post(env.round_id, "COMMENT", (), key="k1", digest="any")
    assert env.github.posted == [] and env.store.postings(env.round_id) == []


async def test_a_body_exactly_at_the_limit_marker_included_is_posted(env: Env) -> None:
    summary = "s" * 60_000
    probe = compose_review(summary, [_outside("src/app.py", "x")], commentable_lines([]))
    fill = GITHUB_BODY_LIMIT - MARKER_RESERVE - len(probe.body) + 1
    exact = [_outside("src/app.py", "x" * fill)]
    await env.service.save_drafts(env.round_id, summary, exact)
    preview = await env.service.preview(env.round_id, "COMMENT")
    assert len(preview.body) + MARKER_RESERVE == GITHUB_BODY_LIMIT
    assert await env.post("COMMENT") == URL
    assert len(str(env.github.posted[0]["body"])) <= GITHUB_BODY_LIMIT
    # One character more is refused.
    await env.service.save_drafts(env.round_id, summary + "s", exact)
    with pytest.raises(ReviewBodyTooLong):
        await env.service.preview(env.round_id, "COMMENT")


def test_the_marker_reserve_covers_the_largest_posting_id() -> None:
    assert len(marked_body("body", 2**63 - 1)) - len("body") == MARKER_RESERVE


# --- Codex round 2: drafts are seeded once the findings are in ---------------------------------


def _running_round(env: Env) -> int:
    """A second round on the head whose triage jobs haven't finished."""
    return env.store.start_round(env.ref, HEAD, "b" * 40, "quick", env.clock())


async def test_drafts_opened_early_get_the_findings_when_they_arrive(env: Env) -> None:
    round_id = _running_round(env)
    early = await env.service.drafts(round_id)
    assert early.findings_pending and early.drafts == ()
    preview = await env.service.preview(round_id, "COMMENT")  # also before the findings
    assert any("still running" in note for note in preview.notes)

    # The author writes a draft of their own meanwhile.
    mine = Draft(path="src/app.py", body="My own note.", line=4, side="RIGHT")
    saved = await env.service.save_drafts(round_id, "Notes.", [mine])
    assert saved.findings_pending and [d.body for d in saved.drafts] == ["My own note."]

    env.store.put_artifact(round_id, "correctness", {"findings": []})
    assert (await env.service.drafts(round_id)).findings_pending  # findings still running
    env.store.put_artifact(round_id, "findings", {"findings": [finding(2, 3, "Swallowed.")]})
    ready = await env.service.drafts(round_id)
    assert not ready.findings_pending
    assert [d.body for d in ready.drafts][0] == "My own note."
    assert ["Swallowed." in d.body for d in ready.drafts] == [False, True]
    assert ready.summary == "Notes."
    # Seeding is once: reading again (or deleting a generated draft) adds nothing back.
    assert len((await env.service.drafts(round_id)).drafts) == 2
    await env.service.save_drafts(round_id, "Notes.", [ready.drafts[0]])
    assert len((await env.service.drafts(round_id)).drafts) == 1


async def test_a_save_that_triggers_seeding_keeps_the_findings(env: Env) -> None:
    """PR 3 final I2: an autosave that is the first request after the findings land must not
    delete the drafts it seeds (the client never saw them)."""
    round_id = _running_round(env)
    assert (await env.service.drafts(round_id)).findings_pending
    env.store.put_artifact(round_id, "correctness", {"findings": []})
    env.store.put_artifact(round_id, "findings", {"findings": [finding(2, 3, "Swallowed.")]})
    mine = Draft(path="src/app.py", body="My own note.", line=4, side="RIGHT")
    saved = await env.service.save_drafts(round_id, "Notes.", [mine])
    assert not saved.findings_pending
    assert [d.body for d in saved.drafts][0] == "My own note."
    assert ["Swallowed." in d.body for d in saved.drafts] == [False, True]
    # Seen now: a later save that leaves the finding draft out deletes it, as before.
    await env.service.save_drafts(round_id, "Notes.", [saved.drafts[0]])
    assert len((await env.service.drafts(round_id)).drafts) == 1


async def test_a_failed_findings_job_still_seeds_what_there_is(env: Env) -> None:
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "correctness", {"findings": [finding(2, 3, "Bug.")]})
    env.store.put_artifact(round_id, "jobs", {"findings": {"state": "failed", "error": "x"}})
    drafts = await env.service.drafts(round_id)
    assert not drafts.findings_pending and len(drafts.drafts) == 1


# --- R54: approving against the evidence needs confirming --------------------------------------


async def test_approving_an_unfinished_round_needs_confirmation(env: Env) -> None:
    round_id = _running_round(env)
    await env.service.save_drafts(round_id, "Looks fine.", [])
    env.store.put_artifact(
        round_id,
        "evidence",
        {"ci": "pending", "correctness": "missing", "correctness_coverage": None},
    )
    preview = await env.service.preview(round_id, "APPROVE")
    assert preview.warnings == ("approve_against_evidence",)
    assert "APPROVE" in preview.allowed_events  # the author still decides
    assert any("hasn't finished" in note for note in preview.notes)
    assert any("coverage read: unknown" in note for note in preview.notes)
    with pytest.raises(NeedsConfirmation) as refused:
        await env.service.post(round_id, "APPROVE", (), key="k1", digest=preview.digest)
    assert refused.value.warnings == ("approve_against_evidence",)
    assert env.github.posted == []
    assert (await env.service.preview(round_id, "COMMENT")).warnings == ()


async def test_approving_against_a_closer_look_verdict_needs_confirmation(env: Env) -> None:
    env.store.set_verdict(env.round_id, "needs_closer_look")
    env.store.put_artifact(
        env.round_id, "verdict", {"verdict": "needs_closer_look", "reasons": ["CI is failing"]}
    )
    env.store.put_artifact(
        env.round_id,
        "evidence",
        {"ci": "failing", "correctness": "missing", "correctness_coverage": {"pct": 40.0}},
    )
    preview = await env.service.preview(env.round_id, "APPROVE")
    assert preview.warnings == ("approve_against_evidence",)
    why = " ".join(preview.notes)
    assert "needs_closer_look" in why and "CI is failing" in why
    assert "CI: failing" in why and "correctness: missing" in why and "40.0%" in why
    with pytest.raises(NeedsConfirmation):
        await env.post("APPROVE")
    assert env.github.posted == []
    # Confirmed, it posts, once.
    assert await env.post("APPROVE", ["approve_against_evidence"]) == URL
    assert await env.post("APPROVE", ["approve_against_evidence"]) == URL
    assert len(env.github.posted) == 1 and env.github.posted[0]["event"] == "APPROVE"


async def test_approving_an_approve_verdict_needs_no_extra_confirmation(env: Env) -> None:
    preview = await env.service.preview(env.round_id, "APPROVE")
    assert "approve_against_evidence" not in preview.warnings
    assert await env.post("APPROVE") == URL


async def test_seeds_that_would_pass_the_draft_limit_are_capped_and_counted(env: Env) -> None:
    """Codex round 2 on PR #58: the author's drafts come first; seeds fill only what is left
    of DRAFTS_MAX, the rest are counted, and the next save is accepted."""
    from jesinia.apps.review.domain.posting import DRAFTS_MAX

    round_id = _running_round(env)
    assert (await env.service.drafts(round_id)).findings_pending
    env.store.put_artifact(round_id, "correctness", {"findings": []})
    many = [finding(2, 3, f"Finding {i}.") for i in range(400)]
    env.store.put_artifact(round_id, "findings", {"findings": many})
    mine = [Draft(path="src/app.py", body=f"Mine {i}.", line=4, side="RIGHT") for i in range(150)]
    saved = await env.service.save_drafts(round_id, "Notes.", mine)
    assert len(saved.drafts) == DRAFTS_MAX
    assert [d.body for d in saved.drafts[:150]] == [f"Mine {i}." for i in range(150)]
    assert saved.seeds_dropped == 400 - (DRAFTS_MAX - 150)
    again = await env.service.save_drafts(round_id, "Notes.", list(saved.drafts))
    # Accepted; the findings that didn't fit stay reported (they are gone for good).
    assert len(again.drafts) == DRAFTS_MAX and again.seeds_dropped == saved.seeds_dropped
    assert (await env.service.drafts(round_id)).seeds_dropped == saved.seeds_dropped


async def test_a_preview_says_when_the_round_was_already_posted(env: Env) -> None:
    """Final M2: posting again from a round already posted (Post all, another tab) adds a
    second review; the preview says so."""
    assert not any(
        "already posted" in n for n in (await env.service.preview(env.round_id, "COMMENT")).notes
    )
    await env.post("COMMENT")
    preview = await env.service.preview(env.round_id, "APPROVE")
    (note,) = [n for n in preview.notes if "already posted" in n]
    assert "COMMENT" in note and URL in note and "second review" in note


# --- seeding per finding kind: a failed job or a stale finish never uses up seeding ------------


def _jobs(**states: str) -> dict[str, dict[str, str | None]]:
    return {kind: {"state": state, "error": None} for kind, state in states.items()}


async def test_should_seed_findings_that_arrive_after_failed_jobs_on_a_finished_round(
    env: Env,
) -> None:
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "jobs", _jobs(correctness="failed", findings="failed"))
    # Job failures never set the round's error: it finishes "cleanly" with no findings.
    env.store.finish_round(round_id, env.clock(), None, "needs_closer_look")
    failed = await env.service.drafts(round_id)
    assert failed.drafts == () and not failed.findings_pending

    # Retry reuses the finished round (its finished_at stays) and reruns the jobs in it.
    env.store.put_artifact(round_id, "jobs", _jobs(correctness="running", findings="running"))
    assert (await env.service.drafts(round_id)).findings_pending
    env.store.put_artifact(round_id, "correctness", {"findings": [finding(2, 3, "Swallowed.")]})
    env.store.put_artifact(round_id, "findings", {"findings": [finding(2, 3, "Error lost.")]})
    env.store.put_artifact(round_id, "jobs", _jobs(correctness="done", findings="done"))
    retried = await env.service.drafts(round_id)
    assert not retried.findings_pending
    assert len(retried.drafts) == 2
    assert all(d.origin_finding == "correctness/important" for d in retried.drafts)


async def test_should_not_reseed_a_kind_after_the_author_deletes_its_drafts(env: Env) -> None:
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "correctness", {"findings": [finding(2, 3, "Swallowed.")]})
    assert len((await env.service.drafts(round_id)).drafts) == 1
    await env.service.save_drafts(round_id, "", [])
    assert (await env.service.drafts(round_id)).drafts == ()
    # The other kind still seeds when it lands, without bringing the deleted one back.
    env.store.put_artifact(round_id, "findings", {"findings": [finding(11, 11, "Off by one.")]})
    (only,) = (await env.service.drafts(round_id)).drafts
    assert "Off by one." in only.body
    await env.service.save_drafts(round_id, "", [])
    assert (await env.service.drafts(round_id)).drafts == ()


async def test_should_seed_each_finding_kind_as_it_arrives(env: Env) -> None:
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "correctness", {"findings": [finding(2, 3, "Swallowed.")]})
    early = await env.service.drafts(round_id)
    assert early.findings_pending and len(early.drafts) == 1
    # The author keeps the seeded draft and adds one; the findings job then lands, and the
    # save that first sees it appends its drafts after the author's list.
    mine = Draft(path="src/app.py", body="My own note.", line=4, side="RIGHT")
    env.store.put_artifact(round_id, "findings", {"findings": [finding(11, 11, "Off by one.")]})
    saved = await env.service.save_drafts(round_id, "", [*early.drafts, mine])
    assert not saved.findings_pending and saved.seeds_dropped == 0
    bodies = [d.body for d in saved.drafts]
    assert "Swallowed." in bodies[0] and bodies[1] == "My own note." and "Off by one." in bodies[2]
    assert len((await env.service.drafts(round_id)).drafts) == 3


async def test_should_not_repeat_a_seed_the_author_already_has_in_their_save(env: Env) -> None:
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "correctness", {"findings": []})
    env.store.put_artifact(round_id, "findings", {"findings": [finding(2, 3, "Swallowed.")]})
    (seed,) = seed_drafts([finding(2, 3, "Swallowed.")])
    saved = await env.service.save_drafts(round_id, "", [seed])
    assert len(saved.drafts) == 1


async def test_should_never_seed_a_frozen_round(env: Env) -> None:
    env.store.put_artifact(env.round_id, "correctness", {"findings": [finding(2, 3, "Late.")]})
    env.store.start_round(env.ref, NEW_HEAD, "b" * 40, "quick", env.clock())
    assert (await env.service.drafts(env.round_id)).drafts == ()


def test_should_claim_each_finding_kind_once(env: Env) -> None:
    store, round_id = env.store, _running_round(env)
    first = Draft("src/app.py", "first", line=2, side="RIGHT", origin_finding="correctness/minor")
    assert store.seed_drafts(round_id, "correctness", [first]) == [first]
    assert store.seed_drafts(round_id, "correctness", [first]) == []  # claimed already
    again = Draft("src/app.py", "first", line=2, side="RIGHT")
    other = Draft("src/app.py", "second", line=3, side="RIGHT")
    # The other kind claims on its own, leaving out a draft repeating one already there.
    assert store.seed_drafts(round_id, "findings", [again, other]) == [other]
    assert [d.body for d in store.drafts(round_id)] == ["first", "second"]
    assert store.seed_drafts(round_id, "findings", [other]) == []


def test_should_cap_seeds_at_the_draft_limit(env: Env) -> None:
    from jesinia.apps.review.domain.posting import DRAFTS_MAX

    store, round_id = env.store, _running_round(env)
    store.save_drafts(round_id, "", [_outside("a.py", f"x{i}") for i in range(DRAFTS_MAX - 1)])
    seeds = [_outside("b.py", "one"), _outside("b.py", "two")]
    assert store.seed_drafts(round_id, "findings", seeds) == seeds[:1]
    assert len(store.drafts(round_id)) == DRAFTS_MAX
    assert store.seeds_dropped(round_id) == 1
    store.save_drafts(round_id, "", store.drafts(round_id), seeds_dropped=2)
    assert store.seeds_dropped(round_id) == 3


def test_rounds_seeded_before_0011_keep_their_seeds_unless_nothing_was_seeded(
    tmp_path: Path,
) -> None:
    """A round seeded before per-kind seeding counts as seeded for the kinds it has, unless
    it holds no finding draft at all: then seeding claimed it before any finding arrived (a
    failed job), and it is seeded again from what is there now."""
    old_schema = tmp_path / "migrations-0010"
    old_schema.mkdir()
    shipped = Path(str(files("jesinia.apps.review.migrations")))
    for sql in sorted(shipped.glob("00*.sql")):
        if sql.name < "0011":
            (old_schema / sql.name).write_text(sql.read_text())
    data = tmp_path / "data"
    before = open_db(data, ModuleDb("review", DbClass.HISTORY, old_schema))
    for round_id, seeded in ((1, 1), (2, 1), (3, 0)):
        before.execute(
            "INSERT INTO rounds (id, repo, number, head_sha, base_sha, depth, started_at, "
            "drafts_seeded) VALUES (?, 'acme/web', 1, ?, ?, 'deep', "
            "'2026-10-01T00:00:00+00:00', ?)",
            (round_id, "a" * 40, "b" * 40, seeded),
        )
        for kind in ("correctness", "findings", "verdict"):
            before.execute(
                "INSERT INTO artifacts (round_id, kind, json) VALUES (?, ?, '{}')",
                (round_id, kind),
            )
    before.execute(
        "INSERT INTO drafts (round_id, path, body, origin_finding) "
        "VALUES (1, 'app.py', 'seeded', 'correctness/minor')"
    )
    before.execute("INSERT INTO drafts (round_id, path, body) VALUES (2, 'app.py', 'mine')")
    before.close()
    after = open_db(data, REVIEW_DB)
    try:
        rows = after.execute("SELECT round_id, kind FROM draft_seeds ORDER BY round_id, kind")
        assert rows.fetchall() == [(1, "correctness"), (1, "findings")]
        assert SqliteReviewStore(after).seeds_dropped(1) == 0
    finally:
        after.close()


async def test_should_report_seeds_the_store_cap_dropped_on_every_read(env: Env) -> None:
    """Codex round 1 on PR #76: a read that seeds past DRAFTS_MAX drops the overflow for
    good, so the count is kept and every draft set (and the preview) reports it."""
    from jesinia.apps.review.domain.posting import DRAFTS_MAX

    round_id = _running_round(env)
    mine = [_outside("a.py", f"Mine {i}.") for i in range(DRAFTS_MAX - 50)]
    await env.service.save_drafts(round_id, "Notes.", mine)
    env.store.put_artifact(round_id, "correctness", {"findings": []})
    many = [finding(2, 3, f"Finding {i}.") for i in range(80)]
    env.store.put_artifact(round_id, "findings", {"findings": many})
    first = await env.service.drafts(round_id)
    assert len(first.drafts) == DRAFTS_MAX and first.seeds_dropped == 30
    assert (await env.service.drafts(round_id)).seeds_dropped == 30
    preview = await env.service.preview(round_id, "COMMENT")
    assert any("30 finding drafts didn't fit" in note for note in preview.notes)
    saved = await env.service.save_drafts(round_id, "Notes.", list(first.drafts))
    assert saved.seeds_dropped == 30


async def test_should_keep_an_interrupted_round_pending_for_finding_jobs_not_run(
    env: Env,
) -> None:
    """Codex round 1 on PR #76: a stop between the finding jobs leaves the round finished
    and interrupted; resuming runs the job that never started (or was cut short)."""
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "correctness", {"findings": [finding(2, 3, "Bug.")]})
    env.store.put_artifact(round_id, "jobs", _jobs(correctness="done"))
    env.store.interrupt_unfinished_rounds(env.clock(), "Jesinia stopped")
    assert (await env.service.drafts(round_id)).findings_pending
    # A job cut short mid-run (its state left "running") is resumed too.
    env.store.put_artifact(round_id, "jobs", _jobs(correctness="done", findings="running"))
    assert (await env.service.drafts(round_id)).findings_pending


async def test_should_not_keep_a_failed_round_pending_for_a_job_it_cut_short(env: Env) -> None:
    """A round that failed outright isn't reused (Retry starts a new one), so a job its
    failure left "running" will never store findings in it."""
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "jobs", _jobs(correctness="failed", findings="running"))
    env.store.finish_round(round_id, env.clock(), "the workspace couldn't be prepared")
    assert not (await env.service.drafts(round_id)).findings_pending


async def test_should_keep_a_clean_round_pending_for_a_finding_job_never_run(env: Env) -> None:
    """A round that finished cleanly is reused when the PR is opened, which runs any
    triage job it lacks, so a finding kind with no state yet is still to come."""
    round_id = _running_round(env)
    env.store.put_artifact(round_id, "correctness", {"findings": []})
    env.store.finish_round(round_id, env.clock(), None, "approve")
    assert (await env.service.drafts(round_id)).findings_pending
