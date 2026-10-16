# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from jesinia.services.agents.application.runner import TurnRequest
from jesinia.services.agents.infrastructure.subprocess_stream import ProcessResult

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


class FakeProc:
    def __init__(self, lines: Sequence[str], code: int = 0, stderr: str = "") -> None:
        self._lines = list(lines)
        self.result = ProcessResult(None, "")
        self._code, self._stderr = code, stderr
        self.argv: list[str] = []
        self.stdin: str | None = None
        self.env: dict[str, str] = {}

    async def lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line
        self.result = ProcessResult(self._code, self._stderr)


def spawn_from(proc: FakeProc):  # type: ignore[no-untyped-def]
    def spawn(argv: list[str], cwd: Path, env: dict[str, str], stdin: str | None) -> FakeProc:
        proc.argv, proc.stdin, proc.env = argv, stdin, env
        return proc

    return spawn


@pytest.fixture
def req(tmp_path: Path) -> TurnRequest:
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jesinia": {
                        "type": "http",
                        "url": "http://127.0.0.1:5374/mcp",
                        "headers": {"Authorization": "Bearer sess-token"},
                    }
                }
            }
        )
    )
    return TurnRequest(
        prompt="What's the core claim?",
        workdir=tmp_path,
        system_prompt="You are helping with a KB page.",
        resume_id=None,
        mcp_config=mcp,
        allowed_tools=["kb_read", "kb_search"],
    )
