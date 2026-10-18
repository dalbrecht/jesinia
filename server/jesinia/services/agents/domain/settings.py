# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which model each runner uses: the saved setting, else the environment, else the CLI's own
default. The name goes into the agent CLI's argv (``-m`` / ``--model``), so it must never be
readable as a flag."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MODEL_NAME_MAX = 100
# Starts with a letter, digit, dot, underscore or colon (never a dash), then the same plus dashes.
MODEL_NAME_PATTERN = rf"^[A-Za-z0-9._:][A-Za-z0-9._:-]{{0,{MODEL_NAME_MAX - 1}}}$"
_MODEL_NAME = re.compile(MODEL_NAME_PATTERN)

ModelSource = Literal["setting", "environment", "default"]


class InvalidModelName(ValueError):  # noqa: N818 - reads as the refusal it is
    """A model name that isn't safe to pass as a single argv value."""


def checked_model_name(value: str) -> str:
    """``value`` unchanged when it is argv-safe.

    Raises:
        InvalidModelName: empty, longer than ``MODEL_NAME_MAX``, starting with a dash, or
            containing anything outside ``[A-Za-z0-9._:-]``.

    Example:
        >>> checked_model_name("gpt-6.1-sol")
        'gpt-6.1-sol'
    """
    if not _MODEL_NAME.fullmatch(value):
        raise InvalidModelName(
            f"a model name is 1 to {MODEL_NAME_MAX} of A-Z a-z 0-9 . _ : - and can't start with -"
        )
    return value


def _safe(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return checked_model_name(value)
    except InvalidModelName:
        return None


@dataclass(frozen=True)
class EffectiveModel:
    setting: str | None  # saved in Settings
    environment: str | None  # from the runner's environment variable, when argv-safe
    model: str | None  # what the runner passes; None leaves the CLI's own default
    source: ModelSource


def effective_model(setting: str | None, environment: str | None) -> EffectiveModel:
    """Setting, then environment, then default. An unsafe environment value is ignored."""
    env = _safe(environment)
    if setting is not None:
        return EffectiveModel(setting, env, setting, "setting")
    if env is not None:
        return EffectiveModel(None, env, env, "environment")
    return EffectiveModel(None, None, None, "default")
