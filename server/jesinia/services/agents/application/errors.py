# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent session errors (names fixed by the plan's interfaces)."""


class SessionBusy(RuntimeError):
    """A turn is already running in this session."""


class SessionNotFound(KeyError):
    """No session with that id."""


class PendingNotFound(KeyError):
    """No pending write with that id in this session."""
