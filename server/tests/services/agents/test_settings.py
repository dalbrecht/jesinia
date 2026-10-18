# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import replace
from pathlib import Path

import pytest

from jesinia.core.db import open_db
from jesinia.services.agents.application.errors import RunnerUnavailable
from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.application.settings import RunnerModels
from jesinia.services.agents.domain.settings import (
    EffectiveModel,
    InvalidModelName,
    checked_model_name,
    effective_model,
)
from jesinia.services.agents.infrastructure.claude import ClaudeCodeRunner
from jesinia.services.agents.infrastructure.codex import CodexRunner
from jesinia.services.agents.infrastructure.settings_store import SqliteModelSettingsStore
from jesinia.services.agents.module import SESSIONS_DB


class MemoryStore:
    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    def models(self) -> dict[str, str]:
        return dict(self.saved)

    def set_model(self, runner: str, model: str | None) -> None:
        if model is None:
            self.saved.pop(runner, None)
        else:
            self.saved[runner] = model


@pytest.mark.parametrize("name", ["gpt-6.1-sol", "claude-opus-5-5", "o3:latest", "a", "x" * 100])
def test_should_accept_argv_safe_model_names(name: str) -> None:
    assert checked_model_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "-m", "--last", "a b", "a/b", "gpt;rm", "x" * 101, "ünï", "a\nb"]
)
def test_should_raise_error_when_model_name_is_not_argv_safe(name: str) -> None:
    with pytest.raises(InvalidModelName):
        checked_model_name(name)


def test_should_prefer_setting_then_environment_then_default() -> None:
    assert effective_model("s1", "e1") == EffectiveModel("s1", "e1", "s1", "setting")
    assert effective_model(None, "e1") == EffectiveModel(None, "e1", "e1", "environment")
    assert effective_model(None, None) == EffectiveModel(None, None, None, "default")


def test_should_fall_back_to_default_when_environment_value_is_unsafe() -> None:
    assert effective_model(None, "--bad").source == "default"
    assert effective_model(None, "").source == "default"


def test_runner_models_precedence_and_partial_updates() -> None:
    env = {"JESINIA_CODEX_MODEL": "env-codex"}
    models = RunnerModels(MemoryStore(), env)
    assert models.effective("codex") == EffectiveModel(
        None, "env-codex", "env-codex", "environment"
    )
    assert models.effective("claude-code").source == "default"

    models.update({"codex": "set-codex"})
    assert models.effective("codex").model == "set-codex"
    assert models.effective("codex").source == "setting"

    models.update({"claude-code": "set-claude"})  # codex omitted: kept
    assert models.effective("codex").model == "set-codex"
    assert models.effective("claude-code").model == "set-claude"

    models.update({"codex": None})  # explicit null: back to the environment
    assert models.effective("codex").source == "environment"
    assert set(models.all()) == {"claude-code", "codex"}


def test_runner_models_refuse_unknown_runners_and_unsafe_names() -> None:
    models = RunnerModels(MemoryStore(), {})
    with pytest.raises(RunnerUnavailable):
        models.update({"fake": "x"})
    with pytest.raises(RunnerUnavailable):
        models.effective("fake")
    with pytest.raises(InvalidModelName):
        models.update({"codex": "-x"})


def test_runner_models_read_the_environment_live() -> None:
    env: dict[str, str] = {}
    models = RunnerModels(MemoryStore(), env)
    env["JESINIA_CLAUDE_MODEL"] = "later"
    assert models.effective("claude-code").model == "later"


def test_runners_pick_up_a_changed_model_without_a_restart(req: TurnRequest) -> None:
    models = RunnerModels(MemoryStore(), {})
    codex = CodexRunner(model_source=models.source_for("codex"))
    claude = ClaudeCodeRunner(model_source=models.source_for("claude-code"))
    assert "-m" not in codex.build_argv(req) and "--model" not in claude.build_argv(req)

    models.update({"codex": "gpt-a", "claude-code": "claude-a"})
    argv = codex.build_argv(req)
    assert argv[argv.index("-m") + 1] == "gpt-a"
    argv = claude.build_argv(replace(req, resume_id="abc"))
    assert argv[argv.index("--model") + 1] == "claude-a"

    models.update({"codex": "gpt-b"})
    argv = codex.build_argv(req)
    assert argv[argv.index("-m") + 1] == "gpt-b"


def test_fixed_model_still_works_without_a_source(req: TurnRequest) -> None:
    argv = CodexRunner(model="fixed").build_argv(req)
    assert argv[argv.index("-m") + 1] == "fixed"


def test_sqlite_store_round_trip(tmp_path: Path) -> None:
    conn = open_db(tmp_path, SESSIONS_DB)
    try:
        store = SqliteModelSettingsStore(conn)
        assert store.models() == {}
        store.set_model("codex", "gpt-a")
        store.set_model("claude-code", "claude-a")
        store.set_model("codex", "gpt-b")
        assert store.models() == {"codex": "gpt-b", "claude-code": "claude-a"}
        store.set_model("codex", None)
        store.set_model("codex", None)  # clearing twice is harmless
        assert SqliteModelSettingsStore(conn).models() == {"claude-code": "claude-a"}
    finally:
        conn.close()


def test_sqlite_store_skips_unreadable_rows(tmp_path: Path) -> None:
    conn = open_db(tmp_path, SESSIONS_DB)
    try:
        conn.execute(
            "INSERT INTO settings (key, json) VALUES ('model.codex', ?), ('model.claude-code', ?)",
            ('"--evil"', "42"),
        )
        assert SqliteModelSettingsStore(conn).models() == {}
    finally:
        conn.close()
