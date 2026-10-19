# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server with a scripted ``fake`` runner, for end-to-end tests (plan Task 25).

    JESINIA_HOME=<tmp> JESINIA_FAKE_RUNNER=1 uvicorn jesinia.testing.fake_runner_app:app

On its first turn in a session the runner calls ``kb_write`` through the session's MCP
endpoint, exactly as a real agent would (so the write gate makes it a pending write),
then replies. Later turns only reply. A tag ask (a prompt starting ``TAGS:``) gets a
fixed JSON tag list and never writes. The fetch service may also reach loopback
addresses, so e2e can serve fixture feeds from a local port. The github service has no
token, so e2e never syncs a real account (Review shows its auth-missing state), unless
``JESINIA_FAKE_GITHUB_URL`` names the e2e fixture GitHub on loopback: then github talks only
to that, with a fixed test token, and ``JESINIA_FAKE_GITHUB_REPOS`` makes the review
workspace fetch from the bare repositories beside it. A review job prompt (``Run the <job>
job.``) is answered with a fixed JSON output for the fixture PR, valid against the job's real
schema. An Issues ask (``Triage the issue.``, then the JSON Schema it must answer) gets a fixed
output valid against that schema (triage, priority, grouping, brief or queue), naming the issues
the ask listed. It refuses to load outside a throwaway ``JESINIA_HOME``, so none of this can ever
activate in a real install.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from jesinia.apps.review.module import ReviewModule
from jesinia.services.agents.application.runner import RunnerStatus, TurnRequest
from jesinia.services.agents.domain.events import (
    AgentEvent,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnDone,
)
from jesinia.services.fetch.domain.address import is_public
from jesinia.services.fetch.module import FetchModule
from jesinia.services.github.module import GitHubModule

DRAFT: dict[str, object] = {
    "title": "Agent draft",
    "folder": "Notes",
    "body": "Drafted by the fake runner.",
}
TAG_REPLY = '["fixture","new-fixture-tag"]'
# Every Issues ask starts "Triage the issue." and then gives the JSON Schema to answer (its
# "title" names it); the triage answer is one readiness suggestion for the fixture issue, valid
# against apps/issues/schemas/triage.json.
TRIAGE_PROMPT = "Triage the issue."
TRIAGE_OUTPUT: dict[str, object] = {
    "labels": [
        {
            "dimension": "readiness",
            "value": "agent-ready",
            "reason": "The steps to reproduce are clear.",
        }
    ],
    "duplicates": [],
    "parent": None,
    "agent_ready": {"ready": True, "missing": [], "reason": "Small and well described."},
}
TRIAGE_REPLY = json.dumps(TRIAGE_OUTPUT)
REPLY = (
    "I read the page. ",
    "It is about the fixture topic. ",
    "I drafted a page for you to review.",
)

Post = Callable[[str, dict[str, str], dict[str, Any]], Any]

_REVIEW_PROMPT = re.compile(r"^Run the (\w+) job\.")
_GREETER = "src/greeter.py"
_GREETER_TEST = "tests/test_greeter.py"
_FINDING = {
    "group": "design",
    "severity": "minor",
    "path": _GREETER,
    "start_line": 5,
    "end_line": 6,
    "explanation": "The fallback greeting is a literal; a named constant would document it.",
    "rule": None,
}
# What each review job answers for the fixture PR (src/greeter.py gains an empty-name guard,
# tests/test_greeter.py is new), each valid against its schema in apps/review/schemas.
REVIEW_JOB_OUTPUTS: dict[str, dict[str, object]] = {
    "overview": {
        "summary": "Greeting an empty name no longer produces a broken greeting.",
        "purpose": "Callers that format a greeting from user input sometimes pass an empty name.",
        "sources": ["the PR description", "src/greeter.py"],
    },
    "background": {
        "sections": [
            {
                "title": "How greetings work today",
                "body": "greet() joins a salutation and the name, whatever the name is.",
                "code": [{"path": _GREETER}],
                "kb": [],
            }
        ]
    },
    "walkthrough": {
        "steps": [
            {
                "title": "Guard the empty name",
                "body": "greet() now answers an empty name with a stranger greeting.",
                "hunks": [{"path": _GREETER, "hunk": 0}],
            },
            {
                "title": "Cover it with a test",
                "body": "A new test pins the behaviour for an empty name.",
                "hunks": [{"path": _GREETER_TEST, "hunk": 0}],
            },
        ],
        "skipped": [],
    },
    "correctness": {
        "summary": "The guard is correct and the new test covers it.",
        "checked": [
            {"path": _GREETER, "start_line": 2, "end_line": 9},
            {"path": _GREETER_TEST, "start_line": 1, "end_line": 5},
        ],
        "findings": [],
        "tests": [{"path": _GREETER_TEST, "name": "test_greet_handles_an_empty_name"}],
    },
    "findings": {"findings": [_FINDING]},
    "rereview": {
        "requests": [
            {
                "comment_ref": "c1",
                "status": "addressed",
                "explanation": "greet() now guards the empty name, as asked.",
                "hunks": [{"path": _GREETER, "hunk": 0}],
            }
        ],
        "new_changes_walkthrough": {
            "steps": [
                {
                    "title": "Guard the empty name",
                    "body": "greet() answers an empty name with a stranger greeting.",
                    "hunks": [{"path": _GREETER, "hunk": 0}],
                }
            ],
            "skipped": [],
        },
    },
    "recommendation": {"verdict": "approve", "reasoning": "A small, tested fix."},
    "metrics": {
        "scores": [
            {
                "criterion": "Tests",
                "rule": {"id": "testing", "layer": "base"},
                "score": 9,
                "note": "The change ships with a test.",
            }
        ]
    },
    "quiz": {
        "questions": [
            {
                "question": f"What does greet('') return after this change in {_GREETER}?",
                "choices": ["Hello, ", "Hello, stranger", "An error"],
                "answer": 1,
                "explanation": "The new guard returns the stranger greeting.",
            },
            {
                "question": "Where is the empty name handled?",
                "choices": ["In farewell()", "In greet()"],
                "answer": 1,
                "explanation": "greet() checks the name before joining.",
            },
            {
                "question": "How is the behaviour pinned?",
                "choices": ["A new test", "A comment", "Nothing"],
                "answer": 0,
                "explanation": f"{_GREETER_TEST} is new.",
            },
        ]
    },
}


def review_job_reply(prompt: str) -> str | None:
    """The JSON a review job prompt (``Run the <job> job. ...``) is answered with, or
    ``None`` when ``prompt`` isn't one of the jobs the fixture PR has an output for."""
    match = _REVIEW_PROMPT.match(prompt)
    output = REVIEW_JOB_OUTPUTS.get(match.group(1)) if match else None
    return None if output is None else json.dumps(output)


_SCHEMA_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
# An issue as the asks' inline context lists it (JSON, indented): its repo, then its number.
_LISTED_ISSUE = re.compile(r'"repo":\s*"([^"]+)",\s*"number":\s*(\d+)')
_QUEUE_MODE = re.compile(r"Use mode `(next|order|split)`")
_QUEUE_NAME = re.compile(r'"name":\s*"([^"]+)"')


def _listed_issues(prompt: str) -> list[dict[str, object]]:
    """The issues an ask's context lists, in order, each once."""
    found = dict.fromkeys((repo, int(n)) for repo, n in _LISTED_ISSUE.findall(prompt))
    return [{"repo": repo, "number": n} for repo, n in found]


def _issues_output(title: str, prompt: str, system_prompt: str) -> dict[str, object]:
    issues = _listed_issues(prompt)
    if title == "priority":
        return {
            "items": [
                {**i, "priority": "priority: high", "rank": rank, "reason": "It unblocks the rest."}
                for rank, i in enumerate(issues, start=1)
            ]
        }
    if title == "grouping":
        group = {
            "title": "Fixture epic",
            "scope": "The listed issues, delivered together.",
            "issues": issues[:2],
            "reason": "They touch the same code.",
        }
        return {"groups": [group] if len(issues) >= 2 else [], "additions": [], "splits": []}
    if title == "brief":
        return {
            "scope": "Deliver the epic's children in order.",
            "acceptance_criteria": ["Every child issue is closed."],
        }
    if title == "queue":
        found = _QUEUE_MODE.search(system_prompt)
        mode = found.group(1) if found else "next"
        queue = _QUEUE_NAME.search(prompt)
        items: list[dict[str, object]] = []
        for position, i in enumerate(issues, start=1):
            item: dict[str, object] = {**i, "reason": "Ready and small."}
            if mode == "order":
                item["position"] = position
            if mode == "split" and queue is not None:
                item["queue"] = queue.group(1)
            items.append(item)
        return {"mode": mode, "items": items}
    return TRIAGE_OUTPUT


def issues_reply(prompt: str, system_prompt: str) -> str | None:
    """The JSON an Issues ask is answered with: valid against the schema the prompt gives (by
    its ``title``; triage without one), or ``None`` when ``prompt`` isn't an Issues ask."""
    if not prompt.startswith(TRIAGE_PROMPT):
        return None
    block = _SCHEMA_BLOCK.search(prompt)
    title = str(json.loads(block.group(1)).get("title", "")) if block else "triage"
    return json.dumps(_issues_output(title, prompt, system_prompt))


async def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


class McpScriptRunner:
    name = "fake"

    def __init__(self, post: Post = _post) -> None:
        self.post = post
        self.turns: dict[str, int] = {}

    async def status(self) -> RunnerStatus:
        return RunnerStatus(True, "fake", "scripted test runner")

    async def _call_kb_write(self, req: TurnRequest) -> bool:
        server = json.loads(req.mcp_config.read_text())["mcpServers"]["jesinia"]
        headers = {
            **server["headers"],
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "kb_write", "arguments": DRAFT},
        }
        result = await self.post(server["url"], headers, payload)
        return not bool(result.get("result", {}).get("isError", True))

    async def run(self, req: TurnRequest) -> AsyncIterator[AgentEvent]:
        key = str(req.workdir)
        if (review := review_job_reply(req.prompt)) is not None:
            yield TextDelta(review)
            yield TurnDone(f"fake-{key[-8:]}", "fake-model")
            return
        if (issues := issues_reply(req.prompt, req.system_prompt)) is not None:
            yield TextDelta(issues)
            yield TurnDone(f"fake-{key[-8:]}", "fake-model")
            return
        if req.prompt.startswith("TAGS:"):
            yield TextDelta(TAG_REPLY)
            yield TurnDone(f"fake-{key[-8:]}", "fake-model")
            return
        turn = self.turns.get(key, 0)
        self.turns[key] = turn + 1
        if turn == 0:
            yield ToolUse("kb_write", DRAFT)
            yield ToolResult("kb_write", await self._call_kb_write(req))
        for chunk in REPLY if turn == 0 else ("Noted. ", "Anything else?"):
            yield TextDelta(chunk)
        yield TurnDone(f"fake-{key[-8:]}", "fake-model")


def _require_throwaway_home() -> None:
    if os.environ.get("JESINIA_FAKE_RUNNER") != "1" or not os.environ.get("JESINIA_HOME"):
        raise RuntimeError(
            "fake_runner_app is for tests only: "
            "set JESINIA_FAKE_RUNNER=1 and a throwaway JESINIA_HOME"
        )


def allow_loopback(address: str) -> bool:
    """Public addresses, plus loopback (where e2e serves fixture feeds); never private
    networks, link-local or metadata addresses."""
    if is_public(address):
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _require_temporary_home() -> None:
    """Loopback is reachable only from a home inside the system temp dir (as e2e makes)."""
    _require_throwaway_home()
    temp = Path(tempfile.gettempdir()).resolve()
    home = Path(os.environ["JESINIA_HOME"]).resolve()
    if home == temp or not home.is_relative_to(temp):
        raise RuntimeError(
            f"fake_runner_app reaches loopback only for a JESINIA_HOME inside the "
            f"temporary directory {temp}, not {home}"
        )


def fixture_fetch_module() -> FetchModule:
    """The fetch service widened to loopback; only under the throwaway-home guard."""
    _require_temporary_home()
    return FetchModule(allowed=allow_loopback)


async def no_github_token() -> str | None:
    """The e2e server's GitHub token source: never one (no ``gh``, no Keychain)."""
    return None


def signed_out_github_module() -> GitHubModule:
    """The github service signed out, so e2e never syncs the developer's own account."""
    return GitHubModule(token=no_github_token)


async def fake_github_token() -> str | None:
    """The one token the fixture GitHub is given: a constant test value, never a real one."""
    return "e2e-token"


class LoopbackGitHubTransport(httpx.AsyncBaseTransport):
    """Sends every request meant for api.github.com to the fixture GitHub instead.

    Args:
        base_url: the fixture server's origin (``http://127.0.0.1:<port>``).
        inner: where the rewritten request actually goes (a real transport by default).
    """

    def __init__(self, base_url: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._target = httpx.URL(base_url)
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self._target.scheme, host=self._target.host, port=self._target.port
        )
        request.headers["host"] = f"{self._target.host}:{self._target.port}"
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _fixture_github_url() -> str:
    """The fixture GitHub's origin from ``JESINIA_FAKE_GITHUB_URL`` (loopback http only)."""
    url = httpx.URL(os.environ["JESINIA_FAKE_GITHUB_URL"])
    if url.scheme != "http" or url.port is None or url.host != "127.0.0.1":
        raise RuntimeError(
            f"the fixture GitHub must be an http loopback origin with a port, not {url}"
        )
    return str(url).rstrip("/")


def github_module() -> GitHubModule:
    """The github service: the fixture GitHub when ``JESINIA_FAKE_GITHUB_URL`` is set (only
    under the temporary-home guard), else signed out."""
    if not os.environ.get("JESINIA_FAKE_GITHUB_URL"):
        return signed_out_github_module()
    _require_temporary_home()
    return GitHubModule(
        token=fake_github_token, transport=LoopbackGitHubTransport(_fixture_github_url())
    )


def review_module() -> ReviewModule:
    """The review app, fetching from the fixture repositories when
    ``JESINIA_FAKE_GITHUB_REPOS`` names their directory (only under the temporary-home guard)."""
    repos = os.environ.get("JESINIA_FAKE_GITHUB_REPOS")
    if not repos:
        return ReviewModule()
    _require_temporary_home()
    return ReviewModule(lambda repo: str(Path(repos) / f"{repo}.git"))


def build_app() -> FastAPI:
    _require_throwaway_home()
    from jesinia.core.app import create_app
    from jesinia.core.config import load_config
    from jesinia.core.module import Module
    from jesinia.core.paths import config_path
    from jesinia.registry import modules
    from jesinia.services.agents.module import AgentsModule

    replacements: dict[str, Callable[[], Module]] = {
        "agents": lambda: AgentsModule(runners={"fake": McpScriptRunner()}),
        "fetch": fixture_fetch_module,
        "github": github_module,
        "review": review_module,
    }
    mods = [replacements[m.name]() if m.name in replacements else m for m in modules()]
    return create_app(load_config(config_path()), mods)


def __getattr__(name: str) -> FastAPI:  # `uvicorn jesinia.testing.fake_runner_app:app`
    if name == "app":
        return build_app()
    raise AttributeError(name)
