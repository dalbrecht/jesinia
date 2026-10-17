# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review agent jobs and the evidence rule (spec §3.2, §5.1). Pure: no I/O.

An agent's output is advice. ``recommend`` decides the verdict: **approve** only when CI on
the head is green and the correctness pass completed without a critical or important
finding, whatever the agent suggested (a PR saying "recommend approve" can at most change
the suggestion, never the rule).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from jesinia.apps.review.domain.gates import Depth
from jesinia.apps.review.domain.workspace import canonical_repo
from jesinia.services.github.domain.pr import CheckState, PrCommit, PullRequest

# The runner review jobs use unless Settings → Review picks another (the wireframe's).
DEFAULT_RUNNER = "codex"

Verdict = Literal["approve", "request_changes", "needs_closer_look"]
VERDICTS: tuple[Verdict, ...] = ("approve", "request_changes", "needs_closer_look")
FindingGroup = Literal["correctness", "design", "style"]
FINDING_GROUPS: tuple[FindingGroup, ...] = ("correctness", "design", "style")
SEVERITIES = ("critical", "important", "minor")
BLOCKING_SEVERITIES = frozenset({"critical", "important"})
CorrectnessOutcome = Literal["clean", "issues", "missing"]

EVIDENCE_RULE = (
    "the evidence rule allows approve only with green CI on the head and a completed "
    "correctness pass without critical or important findings"
)

# ``Co-Authored-By`` trailers naming one of these mark a commit as agent-written.
_TRAILER = re.compile(r"^[ \t]*co-authored-by:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_AGENT_MARKERS = (
    "claude",
    "anthropic",
    "codex",
    "openai",
    "chatgpt",
    "copilot",
    "cursor",
    "devin",
    "gemini",
    "aider",
    "[bot]",
)
# Agents that open PRs from ordinary (non-bot) GitHub accounts.
AGENT_LOGINS = frozenset(
    {"copilot", "copilot-swe-agent", "devin-ai-integration", "codex", "chatgpt-codex-connector"}
)


class ReviewJobError(Exception):
    """Base for a review agent job that could not produce its result."""


class JobKind(StrEnum):
    """What a review agent job produces; each has a JSON schema in ``schemas/``."""

    CORRECTNESS = "correctness"
    FINDINGS = "findings"
    RECOMMENDATION = "recommendation"
    OVERVIEW = "overview"
    BACKGROUND = "background"
    WALKTHROUGH = "walkthrough"
    QUIZ = "quiz"
    METRICS = "metrics"
    STACK_WALKTHROUGH = "stack_walkthrough"
    REREVIEW = "rereview"


@dataclass(frozen=True)
class Round:
    """One review pass over one head: the stored round's id, the PR as fetched for it, its
    commits (as GitHub listed them), the proposed depth and what Jesinia knows of the
    author's familiarity with the code (a summary from the knowledge model)."""

    id: int
    pr: PullRequest
    commits: tuple[PrCommit, ...]
    depth: Depth
    knowledge: str = ""

    @property
    def repo(self) -> str:
        return canonical_repo(self.pr.repo)


def has_agent_trailer(message: str) -> bool:
    """Whether a commit message has a ``Co-Authored-By`` trailer naming an agent or bot."""
    return any(
        marker in who.lower() for who in _TRAILER.findall(message) for marker in _AGENT_MARKERS
    )


def is_agent_author(author: str, author_is_bot: bool) -> bool:
    """Whether a PR author is a bot account or a known agent's account."""
    login = author.lower()
    return author_is_bot or login.endswith("[bot]") or login in AGENT_LOGINS


def correctness_depth(proposed: Depth, agent_authored: bool) -> Depth:
    """Agent-written PRs always get the deep correctness pass (spec §5.1)."""
    return "deep" if agent_authored else proposed


@dataclass(frozen=True)
class Evidence:
    """What the recommendation rests on (spec §3.2); ``as_json`` is the ``evidence``
    artifact, whose ``ci``, ``tests`` and ``findings`` the queue row shows."""

    ci: str
    checks: tuple[tuple[str, str], ...]
    tests: str | None
    findings: Mapping[str, int]
    correctness: CorrectnessOutcome

    def as_json(self) -> dict[str, object]:
        return {
            "ci": self.ci,
            "checks": [{"name": name, "status": status} for name, status in self.checks],
            "tests": self.tests,
            "findings": dict(self.findings),
            "correctness": self.correctness,
        }


@dataclass(frozen=True)
class Recommendation:
    verdict: Verdict
    reasons: tuple[str, ...]
    evidence: Evidence


@dataclass(frozen=True)
class _Finding:
    group: str
    severity: str


def _finding(item: object) -> _Finding | None:
    if not isinstance(item, Mapping):
        return None
    group, severity = item.get("group"), item.get("severity")
    if group not in FINDING_GROUPS or severity not in SEVERITIES:
        return None
    return _Finding(str(group), str(severity))


def _findings(items: object) -> list[_Finding] | None:
    """Every finding in ``items``, or ``None`` if it isn't a list of well-formed ones."""
    if not isinstance(items, list):
        return None
    parsed = [_finding(item) for item in items]
    return None if any(f is None for f in parsed) else [f for f in parsed if f is not None]


def _tests(correctness: Mapping[str, object] | None) -> str | None:
    tests = None if correctness is None else correctness.get("tests")
    if not isinstance(tests, list):
        return None
    if not tests:
        return "no tests found for the change"
    return "1 test covers the change" if len(tests) == 1 else f"{len(tests)} tests cover the change"


def _counts(findings: Iterable[_Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.group] = counts.get(finding.group, 0) + 1
    return {group: counts[group] for group in FINDING_GROUPS if group in counts}


def recommend(
    ci: CheckState | None,
    correctness: Mapping[str, object] | None,
    findings: Sequence[object],
    suggested: object = None,
) -> Recommendation:
    """The verdict for a round, from its evidence; the agent's ``suggested`` verdict is an
    input, never the answer.

    - **approve** only when the agent suggests it **and** CI is passing **and** the
      correctness pass completed with no critical or important finding **and** no
      correctness finding from the findings job is critical or important;
    - **request_changes** whenever the agent suggests it (it never approves anything);
    - otherwise **needs_closer_look**, with the reasons.

    Malformed evidence (a correctness result without a findings list or with no checked
    lines, a finding with an unknown group or severity) counts as missing, never as clean.
    """
    # A pass that reports no checked lines didn't do its job, whatever it found.
    checked = correctness.get("checked") if correctness is not None else None
    pass_findings = (
        _findings(correctness.get("findings"))
        if correctness is not None and isinstance(checked, list) and checked
        else None
    )
    extra = _findings(list(findings))
    blockers: list[str] = []
    state = "none" if ci is None else ci.state
    if state != "passing":
        blockers.append("no CI ran on the head" if state == "none" else f"CI is {state}")
    if pass_findings is None:
        blockers.append("the correctness pass did not complete")
    elif any(f.severity in BLOCKING_SEVERITIES for f in pass_findings):
        blockers.append("the correctness pass found critical or important issues")
    if extra is None:
        blockers.append("the findings could not be read")
    elif any(f.group == "correctness" and f.severity in BLOCKING_SEVERITIES for f in extra):
        blockers.append("a critical or important correctness finding is open")
    outcome: CorrectnessOutcome = (
        "missing"
        if pass_findings is None
        else "issues"
        if any(f.severity in BLOCKING_SEVERITIES for f in pass_findings)
        else "clean"
    )
    evidence = Evidence(
        ci=state,
        checks=() if ci is None else tuple((check.name, check.status) for check in ci.checks),
        tests=_tests(correctness),
        findings=_counts([*(pass_findings or []), *(extra or [])]),
        correctness=outcome,
    )
    agent = suggested if isinstance(suggested, str) and suggested in VERDICTS else None
    if agent == "request_changes":
        return Recommendation(
            "request_changes", ("the agent asks for changes", *blockers), evidence
        )
    if blockers:
        overridden = ["the agent suggested approve; the evidence rule overrides it"]
        reasons = (*blockers, EVIDENCE_RULE, *(overridden if agent == "approve" else []))
        return Recommendation("needs_closer_look", reasons, evidence)
    if agent == "approve":
        passed = ("CI is passing", "the correctness pass found no critical or important issue")
        return Recommendation("approve", passed, evidence)
    why = "the agent asks for a closer look" if agent else "the agent gave no recommendation"
    return Recommendation("needs_closer_look", (why,), evidence)
