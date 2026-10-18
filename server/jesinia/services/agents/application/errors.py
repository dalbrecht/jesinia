# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent session errors (names fixed by the plan's interfaces)."""

from jesinia.services.agents.domain.failures import is_transient_failure


class SessionBusy(RuntimeError):
    """A turn is already running in this session."""


class SessionNotFound(KeyError):
    """No session with that id."""


class PendingNotFound(KeyError):
    """No pending write with that id in this session."""


class RunnerUnavailable(LookupError):
    """No runner by that name is configured."""


class TurnFailed(RuntimeError):
    """The agent's turn ended in an error; the message is the runner's own (or says the
    turn ended with no final message)."""

    @property
    def transient(self) -> bool:
        """Whether the provider only failed for now (capacity, rate limit, a 5xx), so the
        same turn may succeed after a pause."""
        return is_transient_failure(str(self))


class SessionClosed(RuntimeError):
    """The session takes no more turns (a one-shot session after its single turn)."""
