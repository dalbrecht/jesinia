# SPDX-License-Identifier: AGPL-3.0-or-later
"""One PR from the inbox to a posted review, end to end (ruling R53).

Everything is real except the edges: GitHub is a respx fake (inbox search, the PR, its files,
commits, CI, the reviews list and the review write), the repo is a temporary git remote, and
the agent is a scripted runner. The runner reads the PR through the REAL repo tools of its
session (``repo_read``, ``repo_grep``) and answers with schema-valid JSON, so the chain under
test is the module's own: ``QueueService.sync`` → ``Scheduler.tick`` → ``Pipeline.prereview``
→ ``JobRunner`` (sessions bound to the worktree, reads recorded) → ``recommend`` → ``open`` →
drafts → preview → post.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field, replace
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jesinia.apps.review import module as review_module
from jesinia.apps.review.api.tools import RepoGrepIn, RepoReadIn
from jesinia.apps.review.domain.posting import posting_marker
from jesinia.apps.review.domain.queue import PrRef, RepoConfig
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.module import REVIEW_DB, SYNC_JOB, ReviewModule
from jesinia.core.config import Config
from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.core.events import EventBus
from jesinia.core.module import AppContext, CallContext, ToolDef
from jesinia.core.scheduler import Scheduler as CoreScheduler
from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.application.sessions import SessionService
from jesinia.services.agents.domain.events import (
    AgentEvent,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnDone,
)
from jesinia.services.agents.infrastructure.store import SqliteSessionStore
from jesinia.services.github.application.service import GitHubService
from jesinia.services.github.infrastructure.cache import SqliteGitHubCache
from tests.apps.review.fakes import FakeClock

API = "https://api.github.com"
REPO = "acme/web"
REVIEW_URL = "https://github.com/acme/web/pull/1#pullrequestreview-80"
SESSIONS_DB = ModuleDb(
    "sessions", DbClass.HISTORY, package_files("jesinia.services.agents.migrations")
)
GITHUB_DB = ModuleDb("github", DbClass.CACHE, package_files("jesinia.services.github.migrations"))
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
BASE_APP = (
    "def widget():\n    return 1\n\n\n"
    "def helper():\n    return 2\n\n\n"
    "def unused():\n    return 3\n"
)
# Line 2 changes (hunk 0); lines 11-14 are added at the end (hunk 1).
HEAD_APP = (
    BASE_APP.replace("return 1", "return widget_total(1)")
    + "\n\ndef widget_total(value):\n    return value + 1\n"
)
# The pass claims every line of the file; its tools return lines 1-4 and the grep's 2 and 13.
CLAIMED = [{"path": "src/app.py", "start_line": 1, "end_line": 14}]
FINDING = {
    "group": "correctness",
    "severity": "minor",
    "path": "src/app.py",
    "start_line": 13,
    "end_line": 14,
    "explanation": "widget_total has no test.",
    "rule": None,
}
REPLIES: dict[str, dict[str, object]] = {
    "correctness": {"summary": "Read it.", "checked": CLAIMED, "findings": [], "tests": []},
    "findings": {"findings": [FINDING]},
    "recommendation": {"verdict": "approve", "reasoning": "Small and safe."},
    "overview": {"summary": "Adds a total.", "purpose": "Widgets add up.", "sources": ["pr.md"]},
    "walkthrough": {
        "steps": [
            {"title": "Call", "body": "Uses it.", "hunks": [{"path": "src/app.py", "hunk": 0}]},
            {"title": "Add", "body": "Adds it.", "hunks": [{"path": "src/app.py", "hunk": 1}]},
        ],
        "skipped": [],
    },
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass(frozen=True)
class Remote:
    path: Path
    base: str
    head: str
    patch: str  # GitHub's patch of src/app.py


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "main")
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text(BASE_APP)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    base = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "-b", "feature")
    (work / "src" / "app.py").write_text(HEAD_APP)
    git(work, "commit", "-q", "-am", "Add widget_total")
    head = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "main", "feature")
    diff = git(work, "diff", base, head, "--", "src/app.py")
    return Remote(bare, base, head, diff[diff.index("@@") :])


@dataclass
class FakeGitHub:
    """GitHub's REST API for one PR, as respx side effects."""

    remote: Remote
    reviews: list[dict[str, object]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def pull(self) -> dict[str, object]:
        r = self.remote
        return {
            "url": f"{API}/repos/{REPO}/pulls/1", "html_url": f"https://github.com/{REPO}/pull/1",
            "number": 1, "state": "open", "title": "Add widget_total",
            "user": {"login": "alice", "type": "User"}, "body": "Adds a total.",
            "created_at": "2026-10-14T09:30:00Z", "requested_reviewers": [{"login": "me"}],
            "requested_teams": [], "draft": False, "mergeable_state": "clean",
            "head": {"ref": "feature", "sha": r.head, "repo": {"full_name": REPO}},
            "base": {"ref": "main", "sha": r.base, "repo": {"full_name": REPO}},
            "commits": 1, "additions": 4, "deletions": 1, "changed_files": 1,
        }  # fmt: skip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = f"/repos/{REPO}"
        if request.method == "POST" and path == f"{prefix}/pulls/1/reviews":
            return self._create(json.loads(request.content))
        if request.method != "GET":
            self.unknown.append(f"{request.method} {path}")
            return httpx.Response(404, json={"message": "Not Found"})
        answers: dict[str, object] = {
            "/user": {"login": "me", "type": "User"},
            "/user/orgs": [],
            "/search/issues": {"total_count": 1, "incomplete_results": False, "items": [{
                "repository_url": f"{API}/repos/{REPO}", "number": 1,
                "html_url": f"https://github.com/{REPO}/pull/1", "title": "Add widget_total",
                "user": {"login": "alice", "type": "User"}, "state": "open",
                "pull_request": {"url": f"{API}/repos/{REPO}/pulls/1"}, "draft": False,
                "created_at": "2026-10-14T09:30:00Z", "body": None,
            }]},
            f"{prefix}/pulls/1": self.pull(),
            f"{prefix}/pulls/1/files": [{
                "filename": "src/app.py", "status": "modified", "additions": 4,
                "deletions": 1, "changes": 5, "patch": self.remote.patch,
            }],
            f"{prefix}/pulls/1/commits": [{
                "sha": self.remote.head,
                "commit": {"message": "Add widget_total", "author": {
                    "name": "Alice", "email": "a@example.invalid", "date": "2026-10-14T09:00:00Z",
                }},
                "author": {"login": "alice", "type": "User"},
            }],
            f"{prefix}/commits/{self.remote.head}/check-runs": {"total_count": 1, "check_runs": [{
                "id": 1, "name": "ci", "head_sha": self.remote.head, "status": "completed",
                "conclusion": "success", "html_url": "https://github.com/ci",
                "started_at": "2026-10-14T09:00:00Z", "completed_at": "2026-10-14T09:05:00Z",
                "app": {"slug": "github-actions"},
            }]},
            f"{prefix}/commits/{self.remote.head}/status": {
                "state": "success", "sha": self.remote.head, "total_count": 0, "statuses": [],
            },
            f"{prefix}/pulls/1/reviews": self.reviews,
            f"{prefix}/pulls/1/comments": [],
        }  # fmt: skip
        if path not in answers:
            self.unknown.append(f"GET {path}")
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=answers[path])

    def _create(self, payload: dict[str, Any]) -> httpx.Response:
        self.created.append(payload)
        review = {
            "id": 80, "user": {"login": "me", "type": "User"}, "body": payload["body"],
            "state": "COMMENTED", "html_url": REVIEW_URL, "commit_id": payload["commit_id"],
            "submitted_at": "2026-10-16T09:30:00Z",
        }  # fmt: skip
        self.reviews.append(review)
        return httpx.Response(200, json=review)


class ScriptedReviewer:
    """The agent: reads the PR with the session's real repo tools on the correctness job,
    then answers each job with schema-valid JSON."""

    name = "codex"

    def __init__(self, tools: dict[str, ToolDef]) -> None:
        self.tools = tools
        self.jobs: list[str] = []
        self.read: list[object] = []

    async def status(self) -> RunnerStatus:
        return RunnerStatus(True, "scripted", "test reviewer")

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        match = re.search(r"Run the (\w+) job", req.prompt)
        assert match is not None, req.prompt
        job = match.group(1)
        self.jobs.append(job)
        if job == "correctness":
            calls = (
                ("repo_read", RepoReadIn(path="src/app.py", start=1, end=4)),
                ("repo_grep", RepoGrepIn(pattern="widget_total")),
            )
            for name, args in calls:
                assert name in req.allowed_tools
                yield ToolUse(name, args.model_dump())
                # What Jesinia's MCP endpoint runs for this session's token.
                self.read.append(
                    await self.tools[name].handler(args, CallContext(req.workdir.name))
                )
                yield ToolResult(name, True)
        yield TextDelta(json.dumps(REPLIES[job]))
        yield TurnDone(f"scripted-{req.workdir.name[:8]}", "scripted")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(token="t" * 43, data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


@pytest.fixture
def connections() -> Iterator[list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    yield opened
    for conn in opened:
        conn.close()


async def test_one_pr_from_the_inbox_to_one_posted_review(
    cfg: Config,
    tmp_path: Path,
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    connections: list[sqlite3.Connection],
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(review_module, "_utc_now", clock)
    monkeypatch.setattr(review_module, "github_remote_url", lambda repo: str(remote.path))
    github_conn = open_db(tmp_path / "github", GITHUB_DB)
    sessions_conn = open_db(tmp_path / "agents", SESSIONS_DB)
    connections += [github_conn, sessions_conn]

    async def token() -> str | None:
        return "gho_test"

    github = GitHubService(token, SqliteGitHubCache(github_conn))
    module = ReviewModule()
    tools: dict[str, ToolDef] = {tool.name: tool for tool in module.mcp_tools()}
    reviewer = ScriptedReviewer(tools)
    agents = SessionService(
        SqliteSessionStore(sessions_conn),
        {"codex": reviewer},
        tmp_path / "sessions",
        "http://127.0.0.1:5374/mcp",
        lambda: tools,
    )
    jobs = CoreScheduler()
    fake = FakeGitHub(remote)
    with respx.mock(assert_all_mocked=True) as router:
        router.route(host="api.github.com").mock(side_effect=fake)
        await module.start(AppContext(cfg, EventBus(), jobs, {"agents": agents, "github": github}))
        try:
            review_conn = open_db(cfg.data_dir, REVIEW_DB)  # the test's own view of it
            connections.append(review_conn)
            store = SqliteReviewStore(review_conn)
            store.upsert_repo(RepoConfig(REPO, watched=True))
            ref = PrRef(REPO, 1)

            # sync → (debounce) → tick → the pre-review's triage jobs.
            await jobs.run_now(SYNC_JOB)
            clock.advance(11)
            await module.scheduler.tick()
            await module.scheduler.drain()
            assert reviewer.jobs == ["correctness", "findings", "recommendation"]
            found = store.latest_round_on_head(ref, remote.head)
            assert found is not None and found[1].finished and found[1].error is None
            round_id = found[0]

            # M7: only what the tools returned counts; the claimed lines 11, 12 and 14 don't.
            coverage = store.artifact(round_id, "coverage")
            assert isinstance(coverage, Mapping)
            read, claimed = coverage["correctness"], coverage["correctness_claimed"]
            assert isinstance(read, Mapping) and isinstance(claimed, Mapping)
            # Claimed: every added line (the removed one is old-side). Read: not lines 11, 13.
            assert claimed["pct"] > read["pct"] > 0, (claimed, read)
            reads = store.artifact(round_id, "correctness_reads")
            assert isinstance(reads, Mapping)
            ranges = {(r["path"], r["start_line"], r["end_line"]) for r in reads["read"]}
            assert ("src/app.py", 1, 4) in ranges and ("src/app.py", 13, 13) in ranges
            verdict = store.artifact(round_id, "verdict")
            assert isinstance(verdict, Mapping) and verdict["verdict"] == "needs_closer_look"
            assert all("untrusted-data" in str(result) for result in reviewer.read)

            # open → the on-open jobs, on the same round.
            opened = await module.pipeline.open(ref)
            assert opened.state == "started" and opened.round_id == round_id
            await module.pipeline.drain()
            assert reviewer.jobs[3:] == ["overview", "walkthrough"]
            # M4: every job's session was unbound (and closed) when it ended.
            assert module.sessions.bound_trees() == []

            # The author edits the seeded draft, previews and posts.
            assert await module.posting.round_for(ref) == round_id
            drafts = await module.posting.drafts(round_id)
            (seeded,) = drafts.drafts
            assert seeded.body.endswith("widget_total has no test.") and seeded.line == 14
            edited = replace(seeded, body="Please add a test for widget_total.")
            await module.posting.save_drafts(round_id, "One note.", [edited])
            preview = await module.posting.preview(round_id, "COMMENT")
            assert [c.body for c in preview.comments] == ["Please add a test for widget_total."]
            url = await module.posting.post(
                round_id, "COMMENT", (), key="post-1", digest=preview.digest
            )
            assert url == REVIEW_URL

            # Exactly one review went to GitHub, on the round's head, with the marker.
            (created,) = fake.created
            (attempt,) = store.postings(round_id)
            assert created["commit_id"] == remote.head and created["event"] == "COMMENT"
            assert str(created["body"]).endswith(posting_marker(attempt.id))
            # A replay (the same key, or a second click) returns the stored URL, sends nothing.
            again = await module.posting.post(
                round_id, "COMMENT", (), key="post-1", digest=preview.digest
            )
            other = await module.posting.post(
                round_id, "COMMENT", (), key="post-2", digest=preview.digest
            )
            assert again == other == REVIEW_URL and len(fake.created) == 1
            stored = store.round(round_id)
            assert stored is not None and stored.done and stored.posted_review_url == REVIEW_URL
            assert fake.unknown == []
        finally:
            await module.stop()
