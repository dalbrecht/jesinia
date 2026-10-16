# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistence the session service needs; implemented by ``infrastructure.store``."""

from __future__ import annotations

from typing import Protocol

from jesinia.services.agents.domain.session import (
    Origin,
    PendingState,
    PendingWrite,
    Session,
    Status,
    StoredEvent,
)


class SessionStore(Protocol):
    def add_session(self, s: Session) -> None: ...

    def get_session(self, id: str) -> Session | None: ...

    def all_sessions(self) -> list[Session]: ...

    def set_status(self, id: str, status: Status) -> None: ...

    def set_agent_session(self, id: str, agent_session_id: str, model: str | None) -> None: ...

    def append(
        self, id: str, kind: str, data: dict[str, object], origin: Origin
    ) -> StoredEvent: ...

    def events(self, id: str, after_seq: int = 0) -> list[StoredEvent]: ...

    def add_pending(self, p: PendingWrite) -> None: ...

    def get_pending(self, id: str) -> PendingWrite | None: ...

    def resolve_pending(self, id: str, state: PendingState, result: object | None) -> None: ...

    def pending_in_state(self, state: PendingState) -> list[PendingWrite]: ...

    def claim_pending(self, id: str, state: PendingState) -> bool:
        """Move a write from ``pending`` to ``state`` atomically; False if it already left
        ``pending`` (a repeated or racing approve/reject)."""
        ...
