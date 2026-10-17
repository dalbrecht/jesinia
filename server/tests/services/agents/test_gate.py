# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from jesinia.core.auth import Principal
from jesinia.services.agents.application.gate import SessionWriteGate
from jesinia.services.agents.infrastructure.fake import FakeRunner
from tests.services.agents.test_sessions import SUBJ, TOOLS, In, _close_connections, make, pack  # noqa: F401


async def test_gate_decisions(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRunner([]))
    gate = SessionWriteGate(svc)
    open_ = await svc.create(SUBJ, "fake", pack())
    pre = await svc.create(SUBJ, "fake", pack(), preauthorized_tools={"kb_write"})
    assert (await gate.check(TOOLS["kb_write"], In(), Principal("user"))).kind == "allow"
    assert (
        await gate.check(TOOLS["kb_read"], In(), Principal("session", open_.id))
    ).kind == "allow"
    assert (await gate.check(TOOLS["kb_write"], In(), Principal("session", pre.id))).kind == "allow"
    d = await gate.check(TOOLS["kb_write"], In(), Principal("session", open_.id))
    assert d.kind == "pending" and d.pending_id
    assert (await gate.check(TOOLS["kb_write"], In(), Principal("session", None))).kind == "deny"
    assert (await gate.check(TOOLS["kb_write"], In(), Principal("session", "gone"))).kind == "deny"


async def test_read_only_sessions_are_denied_every_write(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRunner([]))
    gate = SessionWriteGate(svc)
    reader = await svc.create(SUBJ, "fake", pack(), read_only=True)
    denied = await gate.check(TOOLS["kb_write"], In(), Principal("session", reader.id))
    assert denied.kind == "deny" and "read-only" in (denied.reason or "")
    assert svc.store.pending_in_state("pending") == []  # nothing for the user to approve
    allowed = await gate.check(TOOLS["kb_read"], In(), Principal("session", reader.id))
    assert allowed.kind == "allow"
