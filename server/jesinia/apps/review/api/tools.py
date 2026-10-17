# SPDX-License-Identifier: AGPL-3.0-or-later
"""The read-only repo MCP tools review agents use to read a PR's code."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from jesinia.apps.review.application.repo_tools import RepoTools
from jesinia.apps.review.domain.repo_access import (
    FILE_LINES_PER_SESSION,
    GREP_MAX_MATCHES,
    GREP_MAX_PATTERN_CHARS,
    READ_LINES_PER_CALL,
)
from jesinia.core.module import CallContext, ToolDef

_PATH = Field(min_length=1, max_length=1024, description="A path relative to the PR's root")


class RepoToolsHost(Protocol):
    @property
    def repo_tools(self) -> RepoTools: ...


class RepoReadIn(BaseModel):
    """`repo_read`: read lines of a file in the PR under review."""

    path: str = _PATH
    start: int | None = Field(default=None, ge=1, description="First line (1-based)")
    end: int | None = Field(default=None, ge=1, description="Last line (inclusive)")


class RepoGrepIn(BaseModel):
    """`repo_grep`: search the PR's files."""

    pattern: str = Field(
        min_length=1,
        max_length=GREP_MAX_PATTERN_CHARS,
        description="An extended regular expression (git grep -E)",
    )
    glob: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Only files matching this glob; without a '/', it matches at any depth",
    )


class RepoLsIn(BaseModel):
    """`repo_ls`: list a directory of the PR."""

    path: str = Field(default=".", min_length=1, max_length=1024, description="A directory")


class RepoGitIn(BaseModel):
    """`repo_git`: read the PR's history with git log, show or blame."""

    cmd: Literal["log", "show", "blame"]
    args: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list,
        max_length=32,
        description="Allowlisted options, then revisions, then '--' and paths",
    )


def build_tools(host: RepoToolsHost) -> Sequence[ToolDef]:
    async def repo_read(args: BaseModel, call: CallContext) -> object:
        a = RepoReadIn.model_validate(args.model_dump())
        return await host.repo_tools.read(call.session_id, a.path, a.start, a.end)

    async def repo_grep(args: BaseModel, call: CallContext) -> object:
        a = RepoGrepIn.model_validate(args.model_dump())
        return await host.repo_tools.grep(call.session_id, a.pattern, a.glob)

    async def repo_ls(args: BaseModel, call: CallContext) -> object:
        a = RepoLsIn.model_validate(args.model_dump())
        return await host.repo_tools.ls(call.session_id, a.path)

    async def repo_git(args: BaseModel, call: CallContext) -> object:
        a = RepoGitIn.model_validate(args.model_dump())
        return await host.repo_tools.git(call.session_id, a.cmd, a.args)

    scope = "Works only in a Jesinia review session, on the PR under review."
    return [
        ToolDef(
            "repo_read",
            f"Read lines of a file in the PR, numbered. At most {READ_LINES_PER_CALL} lines per "
            f"call and {FILE_LINES_PER_SESSION} lines per file per session. Lockfiles, "
            f"generated, vendored and binary files are refused. {scope}",
            RepoReadIn,
            repo_read,
            False,
        ),
        ToolDef(
            "repo_grep",
            f"Search the PR's tracked files with an extended regex, optionally limited by a "
            f"glob. At most {GREP_MAX_MATCHES} matches; matched lines count against the "
            f"per-file allowance. {scope}",
            RepoGrepIn,
            repo_grep,
            False,
        ),
        ToolDef(
            "repo_ls",
            f"List a directory of the PR, with sizes and which files are auto-skipped. {scope}",
            RepoLsIn,
            repo_ls,
            False,
        ),
        ToolDef(
            "repo_git",
            "Read the PR's history: git log (--oneline, --stat, -p, --follow, --name-only, "
            "--name-status, -n <k>, --since=, --author=), show (--oneline, --stat, -p, "
            "--name-only, --name-status, <rev>, <rev>:<path>) or blame (-L <a>,<b>, [<rev>] "
            "<path>). Put paths after '--'. Other options are refused. " + scope,
            RepoGitIn,
            repo_git,
            False,
        ),
    ]
