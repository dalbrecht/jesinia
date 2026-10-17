# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent sessions, transcript events, and pending writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Status = Literal["idle", "running", "failed"]
Origin = Literal["pwa", "terminal"]
PendingState = Literal["pending", "approving", "approved", "rejected", "failed"]


@dataclass(frozen=True)
class Subject:
    kind: str  # "kb" (Notes); later "rss", "pr", "issue"
    ref: str  # e.g. a page id
    title: str


@dataclass(frozen=True)
class Session:
    id: str
    subject: Subject
    runner: str
    agent_session_id: str | None
    status: Status
    created_at: datetime
    model: str | None = None
    preauthorized: frozenset[str] = field(default_factory=frozenset)
    # No write tools at all: none offered to the agent, and the gate denies any it calls.
    read_only: bool = False


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    kind: (
        str  # user_message | text | tool_use | tool_result | turn_done | error | pending_write | …
    )
    data: dict[str, object]
    origin: Origin


@dataclass(frozen=True)
class PendingWrite:
    id: str
    session_id: str
    tool: str
    args: dict[str, object]
    state: PendingState
    result: object | None = None
