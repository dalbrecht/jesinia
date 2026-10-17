# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write confirmation for agent sessions (spec §4.3).

The owner (install token) may call any tool. A Jesinia-launched session may call write
tools only if the app pre-authorized them for that session (e.g. ``notes_save`` when the
user clicked "Save note"); anything else becomes a pending write the user approves or
rejects in the PWA. A read-only session may not write at all: its writes are refused, never
queued for approval. A session limited to named tools (a review job) may call only those:
every other tool is refused, reads included.
"""

from __future__ import annotations

from pydantic import BaseModel

from jesinia.core.auth import Principal
from jesinia.core.module import ToolDef
from jesinia.mcp.server import GateDecision
from jesinia.services.agents.application.sessions import SessionNotFound, SessionService


class SessionWriteGate:
    def __init__(self, sessions: SessionService) -> None:
        self.sessions = sessions

    async def check(self, tool: ToolDef, args: BaseModel, principal: Principal) -> GateDecision:
        if principal.kind == "user":
            return GateDecision("allow")
        try:
            session = (
                None if principal.session_id is None else self.sessions.get(principal.session_id)
            )
        except SessionNotFound:
            session = None
        if session is None:
            # Sessions Jesinia doesn't know (anymore) may still read, as before; never write.
            if not tool.writes:
                return GateDecision("allow")
            reason = "no session" if principal.session_id is None else "unknown session"
            return GateDecision("deny", reason=reason)
        if session.allowed_tools is not None and tool.name not in session.allowed_tools:
            return GateDecision("deny", reason=f"{tool.name} is not allowed in this session")
        if not tool.writes:
            return GateDecision("allow")
        if session.read_only:
            return GateDecision("deny", reason="this session is read-only: it cannot write")
        if tool.name in session.preauthorized:
            return GateDecision("allow")
        pending = await self.sessions.request_write(session.id, tool, args)
        return GateDecision("pending", pending_id=pending.id)
