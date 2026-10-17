# SPDX-License-Identifier: AGPL-3.0-or-later
"""Errors the review app's use cases raise to their callers."""

from __future__ import annotations

from jesinia.apps.review.domain.jobs import JobKind, ReviewJobError


class InvalidAgentOutput(ValueError):
    """An agent's reply isn't one bounded JSON object valid against its job's schema (too
    long, too deep, an absurd number, bad JSON, or a schema violation). The message says
    what is wrong in Jesinia's own words, never echoing the reply, since it is sent back to
    the agent for its one retry."""


class JobFailed(ReviewJobError):
    """A review agent job produced no usable result. ``cause`` is the original failure
    (the agent's invalid output, a git or standards error, the runner's own error), also
    chained as ``__cause__``, so the round can show exactly why."""

    def __init__(self, job: JobKind, cause: BaseException) -> None:
        super().__init__(f"the {job.value} job failed: {cause}")
        self.job = job
        self.cause = cause


class RoundPreparationError(ReviewJobError):
    """A review round couldn't be prepared: the PR, its commits or its diff couldn't be read
    (``cause``, also chained as ``__cause__``), or its head moved on. No job ran."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message if cause is None else f"{message}: {cause}")
        self.cause = cause


class HeadMovedOnError(RoundPreparationError):
    """The PR's head is no longer the one this round was started for; the new head gets a
    round of its own once it settles."""
