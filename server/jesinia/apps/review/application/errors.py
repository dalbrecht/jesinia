# SPDX-License-Identifier: AGPL-3.0-or-later
"""Errors the review app's use cases raise to their callers."""

from __future__ import annotations

from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.jobs import JobKind, ReviewJobError


class InvalidAgentOutput(ReviewError, ValueError):
    """An agent's reply isn't one bounded JSON object valid against its job's schema (too
    long, too deep, an absurd number, bad JSON, or a schema violation). The message says
    what is wrong in Jesinia's own words, never echoing the reply, since it is sent back to
    the agent for its one retry."""


class JobFailed(ReviewJobError):
    """A review agent job produced no usable result. ``cause`` is the original failure
    (the agent's invalid output, a git or standards error, the runner's own error), also
    chained as ``__cause__``, so the round can show exactly why. ``detail``, when given,
    says it in readable words (the runner's own message, with the session as a secondary
    detail) where the cause's own text is terse, such as a bare session id."""

    def __init__(self, job: JobKind, cause: BaseException, detail: str | None = None) -> None:
        super().__init__(f"the {job.value} job failed: {detail or cause}")
        self.job = job
        self.cause = cause
        self.detail = detail


class RoundPreparationError(ReviewJobError):
    """A review round couldn't be prepared: the PR, its commits or its diff couldn't be read
    (``cause``, also chained as ``__cause__``), or its head moved on. No job ran."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message if cause is None else f"{message}: {cause}")
        self.cause = cause


class RoundInterruptedError(RoundPreparationError):
    """The round couldn't go on for a reason that isn't the PR's failure; it is marked
    interrupted (not counted toward the head's limit) and runs again on a later tick."""


class HeadMovedOnError(RoundInterruptedError):
    """The PR's head is no longer the one this round was started for; the new head gets a
    round of its own once it settles."""


class GitHubBusyError(RoundInterruptedError):
    """GitHub was briefly unavailable (rate limited, a 5xx, a timeout) while the round was
    prepared (M2); ``cause`` is GitHub's error. The round is retried later, never failed."""


class KnowledgeSourceError(ReviewError):
    """A source of the knowledge model's signals (the signed-in user's emails, the KB, a
    repo's history) couldn't be read. Ingest is best-effort: the cause is logged and
    reported, and nothing else stops."""
