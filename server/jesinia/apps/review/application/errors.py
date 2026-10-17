# SPDX-License-Identifier: AGPL-3.0-or-later
"""Errors the review app's use cases raise to their callers."""

from __future__ import annotations

from jesinia.apps.review.domain.jobs import JobKind


class InvalidJobOutputError(ValueError):
    """An agent's reply isn't one JSON object valid against its job's schema; the message
    says what is wrong (it is sent back to the agent for its one retry)."""


class JobFailed(Exception):
    """A review agent job produced no usable result. ``cause`` is the original failure
    (the agent's invalid output, a git or standards error, the runner's own error), also
    chained as ``__cause__``, so the round can show exactly why."""

    def __init__(self, job: JobKind, cause: BaseException) -> None:
        super().__init__(f"the {job.value} job failed: {cause}")
        self.job = job
        self.cause = cause
