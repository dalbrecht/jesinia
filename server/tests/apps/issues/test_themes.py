# SPDX-License-Identifier: AGPL-3.0-or-later
"""Themes (spec §5.1, §6.2; plan Task 10; rulings IR4, IR15, IR17, IR22, IR27, IR47, IR49).

A theme is a tag from the KB-wide catalog, kept locally per issue: adding and removing one is
local and immediate (IR15), and a new tag passes the catalog's instant, catalog-only check, so a
drop never waits on an agent. Publishing a theme stages, in the pending tray, the
``theme:<tag>`` label setup step and the label additions, which the write-ahead engine applies
with the tray (never creating a label implicitly, IR4). A theme becomes an epic through one
approved batch, planned like a conversion (IR27)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from jesinia.apps.issues.application.themes import THEME_ISSUES_MAX, ThemeService
from jesinia.apps.issues.domain.changes import (
    AddLabel,
    CreateIssue,
    EnsureLabel,
    Existing,
    InvalidChangeError,
    IssueChange,
    PendingChange,
    PendingNotFoundError,
)
from jesinia.apps.issues.domain.epics import (
    CROSS_REPO_WARNING,
    ConversionOutdatedError,
    NothingToConvertError,
)
from jesinia.apps.issues.domain.themes import (
    InvalidThemeError,
    NothingToPublishError,
    ThemeCatalogError,
    ThemeEpicRunningError,
    ThemeNearDuplicateError,
    ThemeNotFoundError,
    ThemesUnavailableError,
)
from jesinia.apps.issues.domain.triage import IssueNotCachedError, RepoNotWatchedError
from jesinia.apps.issues.infrastructure.theme_store import SqliteThemeStore
from jesinia.apps.issues.module import ISSUES_DB
from jesinia.core.db import open_db
from jesinia.core.events import EventBus
from jesinia.services.github.application.service import GitHubRequestError
from jesinia.services.github.domain.issue import Issue
from jesinia.services.kb.application.errors import NearDuplicate, StaleWrite
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.domain.tag_catalog import CatalogEntry
from jesinia.services.kb.infrastructure.fs import LocalFiles
from jesinia.services.kb.infrastructure.index import SqliteIndex
from tests.apps.issues.test_epics import READY, Epics
from tests.apps.issues.test_sync import OTHER, REPO, issue
from tests.services.kb.conftest import KB_DB

ELSEWHERE = "globex/web"
# Agent and catalog calls a theme must never wait on.
DEADLINE = 5.0


class StallingAdvisor:
    """A tag advisor (an agent run) that never answers and records every ask."""

    def __init__(self) -> None:
        self.asks: list[str] = []
        self._never = asyncio.Event()

    async def suggest(self, text: str, catalog: list[CatalogEntry]) -> list[str]:
        self.asks.append(f"suggest:{text}")
        await self._never.wait()
        return []

    async def same_meaning(self, tag: str, catalog: list[CatalogEntry]) -> str | None:
        self.asks.append(f"same_meaning:{tag}")
        await self._never.wait()
        return None


class Themes(Epics):
    """``Epics`` with the theme service over the KB tag catalog."""

    def __init__(self, conn: sqlite3.Connection, kb: KbService) -> None:
        super().__init__(conn)
        self.advisor = StallingAdvisor()
        self.tags: TagService | None = TagService(kb, self.advisor)
        self.store = SqliteThemeStore(conn)
        self.themes = ThemeService(
            self.store,
            self.world.store,
            self.world.pending,
            lambda: self.tags,
            self.world.clock,
            batches=self.world.applied,
        )

    async def watch(self, repo: str, *issues: Issue) -> None:
        self.github.put(*issues)
        await self.world.sync.configure(repo, watched=True, settings_schema=None)
        await self.world.sync.sync(repo)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "issues", ISSUES_DB)
    yield connection
    connection.close()


@pytest.fixture
def kb(tmp_path: Path) -> Iterator[KbService]:
    root = tmp_path / "kb"
    root.mkdir()
    connection = open_db(tmp_path / "kb-data", KB_DB)
    yield KbService(LocalFiles(root), SqliteIndex(connection), EventBus())
    connection.close()


@pytest.fixture
async def env(conn: sqlite3.Connection, kb: KbService) -> AsyncIterator[Themes]:
    made = Themes(conn, kb)
    yield made
    await made.world.service.stop()
    await made.suggest.stop()


async def _catalog(env: Themes) -> list[str]:
    assert env.tags is not None
    return [info.tag for info in await env.tags.catalog()]


# --- the named tests (plan Task 10 Step 1) ------------------------------------------------------


async def test_theme_uses_catalog_and_never_waits_on_agent(env: Themes) -> None:
    await env.synced(issue(1), issue(2), issue(3))
    assert env.tags is not None
    await env.tags.add("database")  # an approved tag: the advisor would be asked to compare

    # A new tag is checked against the catalog alone and added to it; no agent is asked.
    added = await asyncio.wait_for(
        env.themes.add([(REPO, 1), (REPO, 2)], "#Sync Reliability"), DEADLINE
    )
    assert added.tag == "sync-reliability"
    assert added.added == ((REPO, 1), (REPO, 2))
    assert env.advisor.asks == []
    assert "sync-reliability" in await _catalog(env)
    assert env.themes.of_issue(REPO, 1) == ["sync-reliability"]
    # Local and immediate: nothing staged, nothing written to GitHub (IR15).
    assert env.world.pending.pending() == [] and env.github.writes == []

    # Adding it again adds only what is new; an existing tag is resolved by the catalog.
    again = await asyncio.wait_for(
        env.themes.add([(REPO, 2), (REPO, 3)], "sync-reliability"), DEADLINE
    )
    assert again.added == ((REPO, 3),)

    # A near-duplicate of a catalog tag is refused at once, and nothing is stored.
    with pytest.raises(ThemeNearDuplicateError) as near:
        await asyncio.wait_for(env.themes.add([(REPO, 1)], "databases"), DEADLINE)
    assert near.value.near == ["database"]
    assert env.themes.of_issue(REPO, 1) == ["sync-reliability"]
    assert "databases" not in await _catalog(env)
    assert env.advisor.asks == []

    # Undo removes the local theme only; the catalog entry stays, as in Notes (IR22).
    assert env.themes.remove([(REPO, 1), (REPO, 2)], "sync-reliability") == 2
    assert env.themes.of_issue(REPO, 1) == []
    assert "sync-reliability" in await _catalog(env)
    assert [(t.tag, t.count) for t in env.themes.themes()] == [("sync-reliability", 1)]


async def test_publish_creates_theme_label_via_setup(env: Themes) -> None:
    await env.synced(issue(1, labels=READY), issue(2, labels=READY))
    await env.watch(OTHER, issue(3, repo=OTHER))
    await env.themes.add([(REPO, 1), (REPO, 2), (OTHER, 3)], "sync")

    published = env.themes.publish("sync", REPO)
    assert published.label == "theme:sync"
    assert published.ensure is not None and published.ensure.source == "theme"
    assert published.ensure.target is None
    ensure = published.ensure.change
    assert isinstance(ensure, EnsureLabel) and (ensure.repo, ensure.name) == (REPO, "theme:sync")
    assert [(c.target, c.change, c.source) for c in published.added] == [
        (Existing(REPO, 1), AddLabel("theme:sync"), "theme"),
        (Existing(REPO, 2), AddLabel("theme:sync"), "theme"),
    ]
    # Staged only: the label is created by the setup step when the tray is applied (IR4).
    assert env.github.writes == []
    assert len(env.world.pending.pending()) == 3

    # Publishing again stages nothing more: the tray already holds every step.
    repeat = env.themes.publish("sync", REPO)
    assert repeat.ensure == published.ensure and repeat.added == published.added
    assert len(env.world.pending.pending()) == 3

    applied = await env.world.service.apply_pending(None)
    assert applied.result.complete
    names = env.world.write_names()
    assert names.index("create_label") < names.index("add_labels")
    assert "theme:sync" in env.world.labels_of(1) and "theme:sync" in env.world.labels_of(2)
    assert "theme:sync" not in env.github.issues[(OTHER, 3)].labels

    # Once on GitHub (and synced), there is nothing left to publish.
    await env.world.sync.sync(REPO)
    with pytest.raises(NothingToPublishError):
        env.themes.publish("sync", REPO)


async def test_theme_to_epic(env: Themes) -> None:
    await env.synced(issue(1, labels=READY), issue(2, labels=READY))
    await env.watch(OTHER, issue(3, repo=OTHER, labels=READY))
    await env.watch(ELSEWHERE, issue(4, repo=ELSEWHERE, labels=READY))
    await env.themes.add([(REPO, 1), (REPO, 2), (OTHER, 3), (ELSEWHERE, 4)], "sync")

    plan = env.themes.to_epic("sync", REPO)
    assert isinstance(plan.epic, CreateIssue) and plan.epic.repo == REPO
    assert [(c.target, c.warning) for c in plan.children] == [
        (Existing(REPO, 1), None),
        (Existing(REPO, 2), None),
        (Existing(OTHER, 3), CROSS_REPO_WARNING),
    ]
    assert [s.item for s in plan.skipped] == [f"{ELSEWHERE}#4"]  # another owner's (IR17)
    assert env.github.writes == []  # a plan writes nothing

    started = env.themes.start_epic("sync", REPO, plan.digest, "theme-epic:k1", env.world.service)
    assert started.origin == "epic" and started.status == "running"
    # A second approval while the first runs is refused: one theme, one epic at a time.
    with pytest.raises(ThemeEpicRunningError):
        env.themes.start_epic("sync", REPO, plan.digest, "theme-epic:k2", env.world.service)
    await env.world.service.drain()
    replay = env.themes.start_epic("sync", REPO, plan.digest, "theme-epic:k1", env.world.service)
    assert replay.id == started.id and replay.result.complete
    created = [w for w in env.github.writes if w[0] == "create_issue"]
    assert len(created) == 1 and created[0][1][1] == ()  # the epic, with no labels (IR4)
    epic_n = replay.result.created["epic"].n
    assert env.github.parents == {
        (REPO, 1): (REPO, epic_n),
        (REPO, 2): (REPO, epic_n),
        (OTHER, 3): (REPO, epic_n),
    }
    # Its issues are children now: converting the theme again has nothing to do.
    again = env.themes.to_epic("sync", REPO)
    assert again.children == ()
    with pytest.raises(NothingToConvertError):
        env.themes.start_epic("sync", REPO, again.digest, "theme-epic:k3", env.world.service)


# --- more behaviour -----------------------------------------------------------------------------


async def test_publish_never_creates_the_label_implicitly(env: Themes) -> None:
    await env.synced(issue(1, labels=READY))
    await env.themes.add([(REPO, 1)], "sync")
    published = env.themes.publish("sync", REPO)
    assert published.ensure is not None
    env.world.pending.unstage(published.ensure.id)  # the author undid the setup step
    applied = await env.world.service.apply_pending(None)
    (step,) = applied.result.results
    assert step.state == "failed" and "never creates labels implicitly" in (step.cause or "")
    assert "create_label" not in env.world.write_names()
    assert "theme:sync" not in env.world.labels_of(1)


async def test_publish_skips_issues_already_labelled_and_reuses_a_staged_setup(
    env: Themes,
) -> None:
    await env.synced(
        issue(1, labels=(*READY, "Theme:Sync")), issue(2, labels=READY), issue(3, labels=READY)
    )
    await env.themes.add([(REPO, 1), (REPO, 2)], "sync")
    env.store.add([(REPO, 9)], "sync", env.world.clock())  # themed, then gone from the cache
    published = env.themes.publish("sync", REPO)
    assert [c.target for c in published.added] == [Existing(REPO, 2)]
    assert published.already == ((REPO, 1),)
    # A newly themed issue joins the staged setup step; nothing is staged twice.
    await env.themes.add([(REPO, 3)], "sync")
    again = env.themes.publish("sync", REPO)
    assert again.ensure.id == published.ensure.id
    assert [c.target for c in again.added] == [Existing(REPO, 2), Existing(REPO, 3)]
    assert again.added[0].id == published.added[0].id
    assert len(env.world.pending.pending()) == 3


async def test_a_publish_that_cant_stage_everything_stages_nothing(
    env: Themes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await env.synced(issue(1), issue(2))
    await env.themes.add([(REPO, 1), (REPO, 2)], "sync")
    real = env.world.pending.stage
    calls: list[int] = []

    def flaky(repo: str, n: int, change: IssueChange, source: str) -> PendingChange:
        calls.append(n)
        if n == 2:
            raise InvalidChangeError("refused")
        return real(repo, n, change, source)

    monkeypatch.setattr(env.world.pending, "stage", flaky)
    with pytest.raises(InvalidChangeError, match="refused"):
        env.themes.publish("sync", REPO)
    assert env.world.pending.pending() == [] and calls == [1, 2]

    # Taking back is best effort: a failure there is logged with its cause.
    def gone(pending_id: str) -> None:
        raise PendingNotFoundError(pending_id)

    monkeypatch.setattr(env.world.pending, "unstage", gone)
    with pytest.raises(InvalidChangeError):
        env.themes.publish("sync", REPO)
    assert "unstaging" in caplog.text


async def test_a_label_setup_step_needs_a_watched_repo_and_a_known_source(env: Themes) -> None:
    await env.synced(issue(1))
    ensure = EnsureLabel("acme/unwatched", "theme:x", "fbca04", "Theme #x")
    with pytest.raises(RepoNotWatchedError):
        env.world.pending.stage_label(ensure, "theme")
    with pytest.raises(InvalidChangeError):
        env.world.pending.stage_label(ensure, "elsewhere")
    staged = env.world.pending.stage_label(replace(ensure, repo=REPO), "theme")
    assert env.world.pending.pending() == [staged]  # read back from issues.sqlite


async def test_publish_and_epic_need_themed_issues_in_that_repo(env: Themes) -> None:
    await env.synced(issue(1))
    with pytest.raises(ThemeNotFoundError):
        env.themes.to_epic("sync", REPO)
    with pytest.raises(NothingToPublishError):
        env.themes.publish("sync", REPO)
    await env.themes.add([(REPO, 1)], "sync")
    with pytest.raises(NothingToPublishError):
        env.themes.publish("sync", OTHER)
    with pytest.raises(InvalidThemeError, match=REPO):
        env.themes.to_epic("sync", OTHER)
    with pytest.raises(InvalidThemeError):
        env.themes.publish("x" * 60, REPO)


async def test_an_approval_needs_the_plan_the_author_saw(env: Themes) -> None:
    await env.synced(issue(1), issue(2))
    await env.themes.add([(REPO, 1)], "sync")
    plan = env.themes.to_epic("sync", REPO)
    await env.themes.add([(REPO, 2)], "sync")
    with pytest.raises(ConversionOutdatedError):
        env.themes.start_epic("sync", REPO, plan.digest, None, env.world.service)
    assert env.github.writes == []


async def test_an_unfinished_theme_epic_must_be_retried_not_repeated(env: Themes) -> None:
    await env.synced(issue(1))
    await env.themes.add([(REPO, 1)], "sync")
    plan = env.themes.to_epic("sync", REPO)
    env.github.fail["get:1"] = GitHubRequestError(500, "boom")  # the batch stays incomplete
    started = env.themes.start_epic("sync", REPO, plan.digest, None, env.world.service)
    await env.world.service.drain()
    stored = env.world.pending.batch(started.id)
    assert stored.status == "finished" and not stored.result.complete
    with pytest.raises(ThemeEpicRunningError, match=f"retry batch {started.id}"):
        env.themes.start_epic("sync", REPO, plan.digest, None, env.world.service)


async def test_adding_needs_cached_issues_and_the_tag_catalog(env: Themes) -> None:
    await env.synced(issue(1))
    with pytest.raises(IssueNotCachedError):
        await env.themes.add([(REPO, 1), (REPO, 9)], "sync")
    assert env.themes.of_issue(REPO, 1) == [] and "sync" not in await _catalog(env)
    with pytest.raises(InvalidThemeError):
        await env.themes.add([(REPO, 1)], "no good!")
    with pytest.raises(InvalidThemeError):
        await env.themes.add([], "sync")
    env.tags = None  # the kb service isn't running
    with pytest.raises(ThemesUnavailableError):
        await env.themes.add([(REPO, 1)], "sync")
    # Reads, removing and publishing need no catalog.
    assert env.themes.themes() == [] and env.themes.remove([(REPO, 1)], "sync") == 0


async def test_a_catalog_failure_keeps_its_cause_and_stores_nothing(
    env: Themes, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.synced(issue(1))
    assert env.tags is not None

    async def stale(tags: object) -> list[str]:
        raise StaleWrite("Tags.md changed since it was read")

    monkeypatch.setattr(env.tags, "ensure", stale)
    with pytest.raises(ThemeCatalogError, match="Tags.md changed") as failed:
        await env.themes.add([(REPO, 1)], "sync")
    assert isinstance(failed.value.__cause__, StaleWrite)
    assert env.themes.of_issue(REPO, 1) == []


async def test_theme_reads_list_issues_with_their_cached_state(env: Themes) -> None:
    await env.synced(issue(1), issue(2, state="closed"))
    await env.themes.add([(REPO, 2), (REPO, 1)], "sync")
    await env.themes.add([(REPO, 1)], "auth")
    assert [(t.tag, t.count) for t in env.themes.themes()] == [("auth", 1), ("sync", 2)]
    listed = env.themes.issues("#Sync")
    assert [(i.repo, i.n, i.state) for i in listed] == [(REPO, 1, "open"), (REPO, 2, "closed")]
    assert listed[0].title == "Issue 1"
    assert env.themes.of_issue(REPO, 1) == ["auth", "sync"]
    with pytest.raises(ThemeNotFoundError):
        env.themes.issues("nothing")


async def test_a_tag_the_catalog_gained_meanwhile_is_still_a_near_duplicate(
    env: Themes, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.synced(issue(1))
    assert env.tags is not None

    async def raced(tags: object) -> list[str]:
        raise NearDuplicate("sync", ["synch"])

    monkeypatch.setattr(env.tags, "ensure", raced)
    with pytest.raises(ThemeNearDuplicateError) as near:
        await env.themes.add([(REPO, 1)], "sync")
    assert near.value.near == ["synch"] and isinstance(near.value.__cause__, NearDuplicate)
    assert env.themes.of_issue(REPO, 1) == []


async def test_theme_requests_are_bounded_and_list_uncached_issues(env: Themes) -> None:
    await env.synced(issue(1))
    with pytest.raises(InvalidThemeError, match="at most"):
        env.themes.remove([(REPO, n) for n in range(1, THEME_ISSUES_MAX + 2)], "sync")
    with pytest.raises(InvalidThemeError):
        env.themes.remove([], "sync")
    env.store.add([(REPO, 9)], "sync", env.world.clock())
    (gone,) = env.themes.issues("sync")
    assert (gone.repo, gone.n, gone.title, gone.state, gone.url) == (REPO, 9, None, None, None)


async def test_an_epic_skips_uncached_issues_and_forgets_pruned_batches(env: Themes) -> None:
    await env.synced(issue(1))
    await env.themes.add([(REPO, 1)], "sync")
    env.store.add([(REPO, 9)], "sync", env.world.clock())  # themed, then gone from the cache
    env.store.record_epic("sync", "pruned-batch", env.world.clock())  # its batch is gone
    plan = env.themes.to_epic("sync", REPO)
    assert [c.target for c in plan.children] == [Existing(REPO, 1)]
    assert [(s.item, s.reason) for s in plan.skipped] == [
        (f"{REPO}#9", "not in the cache; sync its repo first")
    ]
    started = env.themes.start_epic("sync", REPO, plan.digest, None, env.world.service)
    await env.world.service.drain()
    assert env.world.pending.batch(started.id).result.complete
    assert env.store.epic_batches("sync") == ["pruned-batch", started.id]
