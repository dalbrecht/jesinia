# SPDX-License-Identifier: AGPL-3.0-or-later
"""``sessions.sqlite`` (history class): sessions, transcripts, pending writes."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from jesinia.core.db import transaction
from jesinia.services.agents.domain.session import (
    Origin,
    PendingState,
    PendingWrite,
    Session,
    Status,
    StoredEvent,
    Subject,
)

_S = (
    "id, subject_kind, subject_ref, subject_title, runner, agent_session_id, model, status, "
    "preauthorized, created_at, read_only, allowed_tools"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _session(r: tuple[object, ...]) -> Session:
    return Session(
        id=str(r[0]),
        subject=Subject(str(r[1]), str(r[2]), str(r[3])),
        runner=str(r[4]),
        agent_session_id=str(r[5]) if r[5] is not None else None,
        model=str(r[6]) if r[6] is not None else None,
        status=r[7],  # type: ignore[arg-type]
        preauthorized=frozenset(json.loads(str(r[8]))),
        created_at=datetime.fromisoformat(str(r[9])),
        read_only=bool(r[10]),
        allowed_tools=None if r[11] is None else frozenset(json.loads(str(r[11]))),
    )


class SqliteSessionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_session(self, s: Session) -> None:
        with transaction(self.conn):
            self.conn.execute(
                f"INSERT INTO sessions ({_S}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s.id,
                    s.subject.kind,
                    s.subject.ref,
                    s.subject.title,
                    s.runner,
                    s.agent_session_id,
                    s.model,
                    s.status,
                    json.dumps(sorted(s.preauthorized)),
                    s.created_at.isoformat(),
                    int(s.read_only),
                    None if s.allowed_tools is None else json.dumps(sorted(s.allowed_tools)),
                ),
            )

    def get_session(self, id: str) -> Session | None:
        r = self.conn.execute(f"SELECT {_S} FROM sessions WHERE id = ?", (id,)).fetchone()
        return _session(r) if r else None

    def all_sessions(self) -> list[Session]:
        return [
            _session(r) for r in self.conn.execute(f"SELECT {_S} FROM sessions ORDER BY created_at")
        ]

    def set_status(self, id: str, status: Status) -> None:
        with transaction(self.conn):
            self.conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, id))

    def set_agent_session(self, id: str, agent_session_id: str, model: str | None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE sessions SET agent_session_id = ?, model = COALESCE(?, model) WHERE id = ?",
                (agent_session_id, model, id),
            )

    def append(self, id: str, kind: str, data: dict[str, object], origin: Origin) -> StoredEvent:
        with transaction(self.conn):
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE session_id = ?", (id,)
            ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO events (session_id, seq, kind, data, origin, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    id,
                    seq,
                    kind,
                    json.dumps(data, default=str),
                    origin,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return StoredEvent(int(seq), kind, data, origin)

    def events(self, id: str, after_seq: int = 0) -> list[StoredEvent]:
        rows = self.conn.execute(
            "SELECT seq, kind, data, origin FROM events "
            "WHERE session_id = ? AND seq > ? ORDER BY seq",
            (id, after_seq),
        )
        return [StoredEvent(int(r[0]), str(r[1]), json.loads(str(r[2])), r[3]) for r in rows]

    def add_pending(self, p: PendingWrite) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO pending_writes "
                "(id, session_id, tool, args, state, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    p.id,
                    p.session_id,
                    p.tool,
                    json.dumps(p.args, default=str),
                    p.state,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_pending(self, id: str) -> PendingWrite | None:
        r = self.conn.execute(
            "SELECT id, session_id, tool, args, state, result FROM pending_writes WHERE id = ?",
            (id,),
        ).fetchone()
        if r is None:
            return None
        result = json.loads(r[5]) if r[5] is not None else None
        return PendingWrite(r[0], r[1], r[2], json.loads(r[3]), r[4], result)

    def pending_in_state(self, state: PendingState) -> list[PendingWrite]:
        rows = self.conn.execute(
            "SELECT id FROM pending_writes WHERE state = ? ORDER BY created_at", (state,)
        )
        found = [self.get_pending(str(r[0])) for r in rows.fetchall()]
        return [p for p in found if p is not None]

    def claim_pending(self, id: str, state: PendingState) -> bool:
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE pending_writes SET state = ? WHERE id = ? AND state = 'pending'",
                (state, id),
            )
        return cur.rowcount == 1

    def resolve_pending(self, id: str, state: PendingState, result: object | None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE pending_writes SET state = ?, result = ? WHERE id = ?",
                (state, json.dumps(result, default=str) if result is not None else None, id),
            )
