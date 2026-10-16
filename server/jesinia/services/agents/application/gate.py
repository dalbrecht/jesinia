# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write confirmation for agent sessions (spec §4.3).

The owner (install token) may call any tool. A Jesinia-launched session may call write
tools only if the app pre-authorized them for that session (e.g. ``notes_save`` when the
user clicked "Save note"); anything else becomes a pending write the user approves or
rejects in the PWA.
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
        if principal.kind == "user" or not tool.writes:
            return GateDecision("allow")
        if principal.session_id is None:
            return GateDecision("deny", reason="no session")
        try:
            session = self.sessions.get(principal.session_id)
        except SessionNotFound:
            return GateDecision("deny", reason="unknown session")
        if tool.name in session.preauthorized:
            return GateDecision("allow")
        pending = await self.sessions.request_write(session.id, tool, args)
        return GateDecision("pending", pending_id=pending.id)
