# SPDX-License-Identifier: AGPL-3.0-or-later
"""The issues module skeleton: owns issues.sqlite (history) and its idempotency keys."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from jesinia.apps.issues.module import IDEMPOTENCY_PRUNE_JOB, ISSUES_DB, IssuesModule
from jesinia.core.config import Config
from jesinia.core.db import DbClass, open_db
from jesinia.core.events import EventBus
from jesinia.core.idempotency import SqliteIdempotencyStore
from jesinia.core.module import AppContext
from jesinia.core.scheduler import Scheduler
from jesinia.registry import DATABASES, modules


def test_issues_db_is_history_and_registered_after_its_services() -> None:
    assert ISSUES_DB.name == "issues" and ISSUES_DB.db_class is DbClass.HISTORY
    assert ISSUES_DB in DATABASES
    names = [m.name for m in modules()]
    assert names.index("issues") > max(names.index(n) for n in ("kb", "agents", "github"))


async def test_start_opens_the_db_and_wires_idempotency(cfg: Config) -> None:
    module = IssuesModule()
    assert module.router() is None
    assert list(module.mcp_tools()) == []
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
