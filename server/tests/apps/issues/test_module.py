# SPDX-License-Identifier: AGPL-3.0-or-later
"""The issues module skeleton: owns issues.sqlite (history) and its idempotency keys."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from jesinia.apps.issues.application.epics import BriefService, EpicService
from jesinia.apps.issues.application.labels import LabelSetupService
from jesinia.apps.issues.application.pending import ApplyService, PendingService
from jesinia.apps.issues.application.queues import QueueService
from jesinia.apps.issues.application.reads import IssueQueries
from jesinia.apps.issues.application.suggest import SuggestService
from jesinia.apps.issues.application.sync import SYNC_INTERVAL, SyncService
from jesinia.apps.issues.domain.schema import DEFAULT_SCHEMA
from jesinia.apps.issues.domain.suggestions import SuggestionFilters
from jesinia.apps.issues.domain.triage import IssueFilters, triage
from jesinia.apps.issues.module import (
    IDEMPOTENCY_PRUNE_JOB,
    ISSUES_DB,
    SUGGEST_JOB,
    SYNC_JOB,
    IssuesModule,
)
from jesinia.core.config import Config
from jesinia.core.db import DbClass, open_db
from jesinia.core.events import EventBus
from jesinia.core.idempotency import SqliteIdempotencyStore
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Job, Scheduler
from jesinia.registry import DATABASES, modules
from jesinia.services.github.application.service import GitHubService, GitHubUnavailable
from tests.apps.issues.test_sync import issue


def test_issues_db_is_history_and_registered_after_its_services() -> None:
    assert ISSUES_DB.name == "issues" and ISSUES_DB.db_class is DbClass.HISTORY
    assert ISSUES_DB in DATABASES
    names = [m.name for m in modules()]
    assert names.index("issues") > max(names.index(n) for n in ("kb", "agents", "github"))


async def test_start_opens_the_db_and_wires_idempotency(cfg: Config) -> None:
    module = IssuesModule()
    assert module.router() is not None
    assert [tool.name for tool in module.mcp_tools()] == [
        "issues_search",
        "issues_get",
        "queues_list",
        "queue_get",
    ]
    assert not module.health().ok
    with pytest.raises(HTTPException, match="not started") as caught:
        _ = module.idempotency
    assert caught.value.status_code == 503
    await module.prune_idempotency_keys()  # nothing to prune before start

    ctx = AppContext(cfg, EventBus(), Scheduler())
    await module.start(ctx)
    assert module.health().ok
    assert (cfg.data_dir / "issues.sqlite").exists()
    assert isinstance(module.idempotency, SqliteIdempotencyStore)
    await ctx.scheduler.run_now(IDEMPOTENCY_PRUNE_JOB)
    await module.stop()
    await module.stop()
    assert not module.health().ok

    # A key a crash left pending would block its retry with 409: start clears it.
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO idempotency (key, route, created_at) VALUES ('k', 'POST /x', '2026-10-16')"
    )
    conn.commit()
    conn.close()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    assert module.idempotency.reserve("k", "POST /x", datetime.now(UTC)) is None
    await module.stop()

    # So would a suggestion an approval claimed when a stop cut it off (IR40).
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO suggestions (id, repo, n, kind, payload_json, reason, state, created_at, "
        "updated_at, claimed_at) VALUES ('s', 'acme/web', 1, 'label', '{}', 'r', 'draft', "
        "'2026-10-16', '2026-10-16', '2026-10-16')"
    )
    conn.commit()
    conn.close()
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    conn = open_db(cfg.data_dir, ISSUES_DB)
    assert conn.execute("SELECT claimed_at FROM suggestions").fetchone() == (None,)
    conn.close()
    await module.stop()


class _NoCache:
    def get(self, key: str) -> None:
        return None

    def put(self, key: str, etag: str, status: int, data: object) -> None:
        return None

    def prune(self, older_than: object) -> int:
        return 0


async def _no_token() -> str | None:
    return None


async def test_sync_needs_the_github_service_and_runs_as_a_job(cfg: Config) -> None:
    module = IssuesModule()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.sync
    for name in ("labels", "queries", "pending", "apply", "epics", "briefs", "queues"):
        with pytest.raises(HTTPException, match="not started"):
            getattr(module, name)
    await module.sync_repos()  # nothing to sync before start

    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    with pytest.raises(HTTPException, match="require the github service") as caught:
        _ = module.sync
    assert caught.value.status_code == 503
    with pytest.raises(HTTPException, match="require the github service"):
        _ = module.labels
    with pytest.raises(HTTPException, match="require the github service"):
        _ = module.apply
    with pytest.raises(HTTPException, match="require the github service"):
        _ = module.briefs  # posting a brief needs GitHub, ...
    assert isinstance(module.epics, EpicService)  # ... the epic views don't
    assert isinstance(module.queries, IssueQueries)  # the cache reads need no GitHub
    assert isinstance(module.pending, PendingService)  # so does the tray
    assert isinstance(module.queues, QueueService)  # queues are local
    assert module.clock().tzinfo is not None
    await module.stop()

    # A batch a crash left running (IR45): start marks it interrupted, for Retry.
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO apply_batches (id, origin, status, batch_json, result_json, created_at, "
        "updated_at) VALUES ('b', 'pending', 'running', ?, ?, '2026-10-16', '2026-10-16')",
        (
            json.dumps({"issues": [], "repo": []}),
            json.dumps(
                {
                    "results": [],
                    "created": {},
                    "stopped": None,
                    "is_rate_limited": False,
                    "rate_limited_until": None,
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    ctx = AppContext(cfg, EventBus(), Scheduler())
    github = GitHubService(_no_token, _NoCache())
    ctx.services["github"] = github
    await module.start(ctx)
    assert isinstance(module.sync, SyncService)
    assert isinstance(module.labels, LabelSetupService)
    assert isinstance(module.apply, ApplyService)
    assert isinstance(module.briefs, BriefService)
    assert module.pending.batch("b").status == "interrupted"
    # Without a token a repo can't be added (GitHub names it), ...
    with pytest.raises(GitHubUnavailable):
        await module.sync.configure("acme/web", watched=True, settings_schema=None)
    # ... and a repo added earlier fails its sync, recorded, while the cache keeps serving.
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO repos (repo, added_at) VALUES ('acme/web', '2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    await ctx.scheduler.run_now(SYNC_JOB)
    state = module.queries.view("inbox", IssueFilters())
    assert state.rows == [] and state.sync_error is not None
    await module.stop()
    await github.aclose()


class _RecordingScheduler(Scheduler):
    def __init__(self) -> None:
        super().__init__()
        self.registered: dict[str, tuple[timedelta, bool]] = {}

    def every(self, name: str, interval: timedelta, job: Job, *, at_start: bool = False) -> None:
        self.registered[name] = (interval, at_start)
        super().every(name, interval, job, at_start=at_start)


async def test_sync_job_runs_every_five_minutes_and_at_start(cfg: Config) -> None:
    assert SYNC_INTERVAL == timedelta(minutes=5)
    scheduler = _RecordingScheduler()
    ctx = AppContext(cfg, EventBus(), scheduler)
    github = GitHubService(_no_token, _NoCache())
    ctx.services["github"] = github
    module = IssuesModule()
    await module.start(ctx)
    assert scheduler.registered[SYNC_JOB] == (timedelta(minutes=5), True)
    await module.stop()
    await github.aclose()


class _Agents:
    """The agents service's ``one_shot``, as the module finds it in ``ctx.services``."""

    def __init__(self) -> None:
        self.asks: list[str] = []

    async def one_shot(
        self, runner: str, pack: object, prompt: str, *, timeout: float = 45.0
    ) -> str:
        self.asks.append(runner)
        return json.dumps(
            {
                "labels": [{"dimension": "priority", "value": "priority: high", "reason": "r"}],
                "duplicates": [],
                "parent": None,
                "agent_ready": {"ready": False, "missing": [], "reason": "r"},
            }
        )


async def test_suggestions_start_with_the_module_and_ask_the_agents_service(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    module = IssuesModule()
    with pytest.raises(HTTPException, match="not started"):
        _ = module.suggest
    await module.run_suggestions()  # nothing to run before start

    # A run a stop cut off is queued again at start.
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO repos (repo, added_at) VALUES ('acme/web', '2026-10-01T00:00:00+00:00')"
    )
    issue_json = json.dumps(
        {
            "repo": "acme/web", "number": 1, "id": 11, "title": "t", "body": "b", "state": "open",
            "state_reason": None, "labels": ["bug"], "milestone": None, "parent": None,
            "created_at": "2026-10-02T00:00:00.000000+00:00",
            "updated_at": "2026-10-02T00:00:00.000000+00:00", "url": "u", "author": "a",
            "linked_pr_state": None, "sub_issue_count": 0,
        }
    )  # fmt: skip
    triage_json = json.dumps(
        {
            "complete": False, "missing": ["readiness", "priority", "area"], "conflicts": [],
            "is_epic": False, "readiness": None, "priority": None, "type": "bug", "areas": [],
        }
    )  # fmt: skip
    conn.execute(
        "INSERT INTO issues (repo, number, json, complete, missing_json, triage_json, is_epic, "
        "state, created_at, updated_at, first_seen_at, inbox) VALUES ('acme/web', 1, ?, 0, '[]', "
        "?, 0, 'open', 'x', 'x', 'x', 1)",
        (issue_json, triage_json),
    )
    conn.execute(
        "INSERT INTO suggestion_runs (repo, n, status, source, due_at, updated_at) VALUES "
        "('acme/web', 1, 'running', 'inbox', '2026-10-02T00:00:00.000000+00:00', 'x')"
    )
    conn.commit()
    conn.close()

    scheduler = _RecordingScheduler()
    ctx = AppContext(cfg, EventBus(), scheduler)
    agents = _Agents()
    ctx.services["agents"] = agents
    ctx.services["agents.runner"] = lambda: "claude-code"
    with caplog.at_level(logging.INFO):
        await module.start(ctx)
    assert "queued 1 issues suggestion runs again" in caplog.text
    assert scheduler.registered[SUGGEST_JOB] == (timedelta(minutes=1), True)
    assert isinstance(module.suggest, SuggestService)
    await ctx.scheduler.run_now(SUGGEST_JOB)
    assert agents.asks == ["claude-code"]
    found = module.suggest.suggestions(SuggestionFilters("acme/web", 1))
    assert [s.payload["value"] for s in found] == ["priority: high"]
    await module.stop()

    # Without the agents service (or a runner), runs wait.
    ctx = AppContext(cfg, EventBus(), Scheduler())
    ctx.services["agents.runner"] = "not callable"
    await module.start(ctx)
    module.suggest.edit(found[0].id, {"dimension": "priority", "value": "priority: low"}, None)
    await module.suggest.suggest_backlog([("acme/web", 1)])
    await module.suggest.drain()
    run = module.suggest.run_state("acme/web", 1)
    assert run is not None and run.status == "waiting"
    await module.stop()


async def test_the_triage_hook_marks_closed_queue_items_done(cfg: Config) -> None:
    closed = issue(1, state="closed")
    triaged = [(closed, triage(closed.labels, 0, DEFAULT_SCHEMA))]
    module = IssuesModule()
    module.on_triaged("acme/web", triaged)  # before start: nothing to tell
    await module.start(AppContext(cfg, EventBus(), Scheduler()))
    queue = module.queues.create("Mine").queue.id
    conn = open_db(cfg.data_dir, ISSUES_DB)
    conn.execute(
        "INSERT INTO queue_items (queue_id, kind, ref, position, added_at) "
        "VALUES (?, 'issue', 'acme/web#1', 0, '2026-10-01T00:00:00+00:00')",
        (queue,),
    )
    conn.commit()
    conn.close()
    module.on_triaged("acme/web", triaged)
    assert [d.item.ref.n for d in module.queues.queue(queue).done] == [1]
    await module.stop()
