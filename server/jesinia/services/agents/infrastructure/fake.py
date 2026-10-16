# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scripted runner for tests and CI (``JESINIA_FAKE_RUNNER=1``, plan Task 25)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.domain.events import AgentEvent


class FakeRunner:
    name = "fake"

    def __init__(self, script: Sequence[AgentEvent], delay_ms: int = 0) -> None:
        self.script = list(script)
        self.delay_ms = delay_ms
        self.last_req: TurnRequest | None = None
        self.requests: list[TurnRequest] = []

    async def status(self) -> RunnerStatus:
        return RunnerStatus(True, "fake", "scripted test runner")

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.last_req = req
        self.requests.append(req)
        for event in self.script:
            if self.delay_ms:
                await asyncio.sleep(self.delay_ms / 1000)
            yield event
