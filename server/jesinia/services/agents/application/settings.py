# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Agents: the model each runner uses, read fresh for every turn."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from jesinia.services.agents.application.errors import RunnerUnavailable
from jesinia.services.agents.application.ports import ModelSettingsStore
from jesinia.services.agents.domain.settings import (
    EffectiveModel,
    checked_model_name,
    effective_model,
)

# The runners whose model can be chosen, and the environment variable each falls back to.
RUNNER_MODEL_ENV: Mapping[str, str] = {
    "claude-code": "JESINIA_CLAUDE_MODEL",
    "codex": "JESINIA_CODEX_MODEL",
}


class RunnerModels:
    """Each runner's model: the saved setting, else its environment variable, else default.

    ``environ`` is read on every lookup (pass ``os.environ``), and so is the store, so a change
    applies to the next turn without a restart.
    """

    def __init__(self, store: ModelSettingsStore, environ: Mapping[str, str]) -> None:
        self._store = store
        self._environ = environ

    def effective(self, runner: str) -> EffectiveModel:
        """Raises ``RunnerUnavailable`` for a runner whose model can't be chosen."""
        env = RUNNER_MODEL_ENV.get(runner)
        if env is None:
            raise RunnerUnavailable(runner)
        return effective_model(self._store.models().get(runner), self._environ.get(env))

    def all(self) -> dict[str, EffectiveModel]:
        return {runner: self.effective(runner) for runner in sorted(RUNNER_MODEL_ENV)}

    def update(self, changes: Mapping[str, str | None]) -> dict[str, EffectiveModel]:
        """Save each runner named in ``changes`` (None clears it); the others keep theirs.

        Raises:
            RunnerUnavailable: a runner whose model can't be chosen (nothing is saved).
            InvalidModelName: a name that isn't argv-safe (nothing is saved).
        """
        for runner, model in changes.items():
            if runner not in RUNNER_MODEL_ENV:
                raise RunnerUnavailable(runner)
            if model is not None:
                checked_model_name(model)
        for runner, model in changes.items():
            self._store.set_model(runner, model)
        return self.all()

    def source_for(self, runner: str) -> Callable[[], str | None]:
        """What a runner calls to learn its model for the turn it is about to start."""
        return lambda: self.effective(runner).model
