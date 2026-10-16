# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent session errors (names fixed by the plan's interfaces)."""


class SessionBusy(RuntimeError):
    """A turn is already running in this session."""


class SessionNotFound(KeyError):
    """No session with that id."""


class PendingNotFound(KeyError):
    """No pending write with that id in this session."""


class RunnerUnavailable(LookupError):
    """No runner by that name is configured."""


class TurnFailed(RuntimeError):
    """The agent's turn ended in an error; the message is the runner's own."""


class SessionClosed(RuntimeError):
    """The session takes no more turns (a one-shot session after its single turn)."""
