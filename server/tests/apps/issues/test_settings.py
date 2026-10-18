# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Issues' own settings (plan Task 14): the runner that answers suggestion asks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jesinia.apps.issues.application.settings import IssuesSettingsService
from jesinia.apps.issues.infrastructure.store import SqliteIssuesStore
from jesinia.apps.issues.module import ISSUES_DB
from jesinia.core.db import open_db


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteIssuesStore]:
    connection: sqlite3.Connection = open_db(tmp_path / "issues", ISSUES_DB)
    yield SqliteIssuesStore(connection)
    connection.close()


def test_should_use_the_agents_preferred_runner_until_one_is_chosen(
    store: SqliteIssuesStore,
) -> None:
    preferred: list[str | None] = ["claude-code"]
    service = IssuesSettingsService(store, lambda: preferred[0])
    assert service.settings().runner is None
    assert service.runner() == "claude-code"
    preferred[0] = None
    assert service.runner() is None  # no runner at all: suggestions wait


def test_should_ask_the_chosen_runner_and_keep_the_choice(store: SqliteIssuesStore) -> None:
    service = IssuesSettingsService(store, lambda: "claude-code")
    assert service.update(runner="codex").runner == "codex"
    assert service.runner() == "codex"
    # Stored: a new service over the same database (a restart) keeps the choice.
    assert IssuesSettingsService(store, lambda: None).runner() == "codex"


def test_should_go_back_to_the_preferred_runner_when_the_choice_is_cleared(
    store: SqliteIssuesStore,
) -> None:
    service = IssuesSettingsService(store, lambda: "claude-code")
    service.update(runner="codex")
    assert service.update(runner=None).runner is None
    assert service.runner() == "claude-code"
