# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stacks of PRs reviewed as one unit (spec §4.3). Pure: no I/O.

**The stack rule:** PRs form a stack when each PR's base branch is another open PR's head
branch, in the same repo, and that head branch lives in the base repository (so a fork PR
never stacks onto a same-named branch). A stack is ordered bottom-up: the PR whose base is
no other PR's head first.

The spec gives no tie-break for a group that isn't a simple chain, so such a group is never
treated as a stack: each of its PRs is shown alone, with the reason (a fork, where two PRs
build on one branch; two open PRs sharing a head branch, so the parent is ambiguous; a
cycle; or more than ``STACK_MAX`` PRs).

Also here: the findings a stack review calls out as cross-PR, the stack walkthrough's
filter (only PRs and hunks Jesinia knows), and **Post all**'s order and per-PR attempt keys.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.queue import PrRef
from jesinia.services.github.domain.pr import ReviewEvent

# More PRs than this in one chain are shown alone (the stack walkthrough takes 50 PRs, and a
# review of more than this many is no longer one unit anyone reads).
STACK_MAX = 20
_GROUPS = frozenset({"correctness", "design", "style"})
_SEVERITIES = frozenset({"critical", "important", "minor"})


class StackError(ReviewError):
    """Base for stack reviews Jesinia refuses or can't finish."""


class NotAStackError(StackError):
    """The PR isn't part of a stack (alone, or in a group the stack rule refuses)."""


class StackPreparationError(StackError):
    """The stack couldn't be read for its jobs: a PR moved on since the stack was read, or
    GitHub or git failed (``__cause__``)."""


class InvalidPostAllError(StackError):
    """A **Post all** request that names nothing, a PR outside the stack, or a PR twice."""


class RoundNotOfPr(StackError):  # noqa: N818 - the error code clients see
    """A Post all item names a round of another PR than its own number (Codex P2 on PR
    #63): refused before anything, never reported as posted. ``number`` is the item's PR,
    ``round_id`` the round it named."""

    def __init__(self, number: int, round_id: int) -> None:
        super().__init__(f"round {round_id} isn't a review round of #{number}; nothing was posted")
        self.number = number
        self.round_id = round_id


class StackMember(Protocol):
    """What the stack rule reads of a PR (``PullRequest`` and ``PrSnapshot`` both have it)."""

    @property
    def repo(self) -> str: ...

    @property
    def number(self) -> int: ...

    @property
    def base_ref(self) -> str: ...

    @property
    def head_ref(self) -> str: ...

    @property
    def head_repo(self) -> str | None: ...

    @property
    def head_sha(self) -> str: ...


# Stacks whose top head doesn't contain every member head (R77), by (repo, signature): the
# reason each PR is shown alone. Kept per set of heads, so a restack is checked again.
Restacks = Mapping[tuple[str, str], str]


def restack_reason(lower: int, top: int) -> str:
    """Why a chain isn't a stack while #``lower``'s head isn't in #``top``'s (R77)."""
    return f"the stack needs a restack: #{lower}'s latest commits aren't in #{top}"


@dataclass(frozen=True)
class RefusedGroup:
    """PRs linked by the stack rule that don't form one chain, and why."""

    repo: str
    numbers: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class StackDetection[M: StackMember]:
    """Every stack (bottom-up) and every refused group among a set of open PRs."""

    stacks: tuple[tuple[M, ...], ...]
    refused: tuple[RefusedGroup, ...]

    def stack_of(self, repo: str, number: int) -> tuple[M, ...] | None:
        """The stack holding ``repo#number``, if any."""
        key = repo.lower()
        for stack in self.stacks:
            if stack[0].repo.lower() == key and any(pr.number == number for pr in stack):
                return stack
        return None

    def refusal_of(self, repo: str, number: int) -> str | None:
        """Why ``repo#number``'s group isn't a stack, if it is in a refused one."""
        key = repo.lower()
        for group in self.refused:
            if group.repo == key and number in group.numbers:
                return group.reason
        return None


def _refs(numbers: Sequence[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def _and(numbers: Sequence[int]) -> str:
    listed = [f"#{n}" for n in numbers]
    return listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"


class _Groups:
    """Union-find over PR numbers: the groups the stack rule links."""

    def __init__(self, numbers: Collection[int]) -> None:
        self._parent = {n: n for n in numbers}

    def find(self, n: int) -> int:
        while self._parent[n] != n:
            self._parent[n] = self._parent[self._parent[n]]
            n = self._parent[n]
        return n

    def join(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)

    def members(self) -> list[list[int]]:
        grouped: dict[int, list[int]] = {}
        for n in sorted(self._parent):
            grouped.setdefault(self.find(n), []).append(n)
        return [group for group in grouped.values() if len(group) > 1]


def _repo_stacks[M: StackMember](
    repo: str, prs: Sequence[M], restack: Restacks
) -> tuple[list[tuple[M, ...]], list[RefusedGroup]]:
    by_number = {pr.number: pr for pr in prs}
    # A PR's head branch can be built on only when it lives in this repo.
    heads: dict[str, list[int]] = {}
    for pr in prs:
        if pr.head_repo is not None and pr.head_repo.lower() == repo:
            heads.setdefault(pr.head_ref, []).append(pr.number)
    # A PR never builds on itself (GitHub refuses a base equal to the head anyway).
    parents = {
        pr.number: sorted(n for n in heads.get(pr.base_ref, []) if n != pr.number) for pr in prs
    }
    groups = _Groups(by_number)
    for child, candidates in parents.items():
        for parent in candidates:
            groups.join(child, parent)
    found: list[tuple[M, ...]] = []
    refused: list[RefusedGroup] = []
    for group in groups.members():
        reason = _why_not_a_chain(group, parents)
        if reason is None:
            chain = _chain(group, parents)
            members = tuple(by_number[n] for n in chain)
            signature = stack_signature([(pr.number, pr.head_sha) for pr in members])
            if len(chain) > STACK_MAX:
                reason = f"the chain has more than {STACK_MAX} PRs, so each is shown alone"
            elif (repo, signature) in restack:
                reason = restack[(repo, signature)]
            else:
                found.append(members)
                continue
        refused.append(RefusedGroup(repo, tuple(group), reason))
    return found, refused


def _why_not_a_chain(group: list[int], parents: Mapping[int, list[int]]) -> str | None:
    """Why a linked group isn't one chain (each PR alone then), or ``None`` if it is."""
    for n in group:
        if len(parents[n]) > 1:
            return (
                f"{_and(parents[n])} share the branch #{n} builds on, so which one it stacks "
                "onto is ambiguous"
            )
    for n in group:
        children = [c for c in group if n in parents[c]]
        if len(children) > 1:
            return f"{_and(children)} both build on #{n} (a fork), so the group isn't one stack"
    if not any(not parents[n] for n in group):
        return f"{_refs(group)} build on each other in a cycle"
    return None


def _chain(group: list[int], parents: Mapping[int, list[int]]) -> list[int]:
    """A checked chain, bottom-up (one root, each PR at most one child, no cycle)."""
    child_of = {parents[n][0]: n for n in group if parents[n]}
    at = next(n for n in group if not parents[n])
    chain = [at]
    while at in child_of:
        at = child_of[at]
        chain.append(at)
    return chain


def detect_stacks[M: StackMember](
    prs: Sequence[M], restack: Restacks | None = None
) -> StackDetection[M]:
    """The stacks among open PRs ``prs`` (bottom-up, ordered by repo then bottom PR) and the
    groups refused as stacks; the caller passes open PRs only. ``restack``: chains whose top
    head was found not to contain every member head (R77), refused with their reason."""
    by_repo: dict[str, list[M]] = {}
    for pr in prs:
        by_repo.setdefault(pr.repo.lower(), []).append(pr)
    found: list[tuple[M, ...]] = []
    refused: list[RefusedGroup] = []
    for repo in sorted(by_repo):
        repo_found, repo_refused = _repo_stacks(repo, by_repo[repo], restack or {})
        found += repo_found
        refused += repo_refused
    found.sort(key=lambda stack: (stack[0].repo.lower(), stack[0].number))
    return StackDetection(tuple(found), tuple(refused))


def stacks[M: StackMember](prs: Sequence[M], restack: Restacks | None = None) -> list[list[M]]:
    """Every stack among open PRs ``prs``, each ordered bottom-up per the stack rule."""
    return [list(stack) for stack in detect_stacks(prs, restack).stacks]


def stack_numbers[M: StackMember](
    prs: Sequence[M], restack: Restacks | None = None
) -> dict[PrRef, tuple[int, ...]]:
    """Each stacked PR's stack, as bottom-up PR numbers."""
    out: dict[PrRef, tuple[int, ...]] = {}
    for stack in detect_stacks(prs, restack).stacks:
        chain = tuple(pr.number for pr in stack)
        for pr in stack:
            out[PrRef(pr.repo, pr.number)] = chain
    return out


def stack_signature(members: Sequence[tuple[int, str]]) -> str:
    """A stack review's identity: its PRs and their heads, bottom-up. Any push to any PR
    makes a new stack review (the old one stays readable)."""
    return ",".join(f"{number}@{head}" for number, head in members)


# --- cross-PR findings ------------------------------------------------------------------------

CrossSource = Literal["shared_file", "agent"]


@dataclass(frozen=True)
class CrossFinding:
    """A finding that concerns more than one PR of the stack. ``shared_file``: a PR's own
    finding on a file another PR of the stack also changes (``found_in`` is its PR);
    ``agent``: one the stack walkthrough reported across PRs."""

    prs: tuple[int, ...]
    group: str
    severity: str
    explanation: str
    source: CrossSource
    found_in: int | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    note: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "prs": list(self.prs),
            "group": self.group,
            "severity": self.severity,
            "explanation": self.explanation,
            "source": self.source,
            "found_in": self.found_in,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "note": self.note,
        }


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def shared_file_findings(
    findings_by_pr: Mapping[int, Sequence[object]], paths_by_pr: Mapping[int, Collection[str]]
) -> list[CrossFinding]:
    """Each PR's findings on a file another PR of the stack also changes, in stack order
    (``findings_by_pr``'s order). Malformed findings are skipped."""
    out: list[CrossFinding] = []
    for number, findings in findings_by_pr.items():
        for item in findings:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            if not isinstance(path, str):
                continue
            others = [n for n, paths in paths_by_pr.items() if n != number and path in paths]
            if not others:
                continue
            out.append(
                CrossFinding(
                    prs=tuple(sorted({number, *others})),
                    group=str(item.get("group", "")),
                    severity=str(item.get("severity", "")),
                    explanation=str(item.get("explanation", "")),
                    source="shared_file",
                    found_in=number,
                    path=path,
                    start_line=_int(item.get("start_line")),
                    end_line=_int(item.get("end_line")),
                    note=f"found in #{number}; {path} is also changed by {_refs(others)}",
                )
            )
    return out


def agent_cross_findings(stack_walkthrough: object) -> list[CrossFinding]:
    """The cross-PR findings a stored (already filtered) stack walkthrough holds."""
    items = (
        stack_walkthrough.get("cross_findings") if isinstance(stack_walkthrough, Mapping) else None
    )
    out: list[CrossFinding] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("prs"), list):
            continue
        prs = tuple(n for n in item["prs"] if _int(n) is not None)
        out.append(
            CrossFinding(
                prs=prs,
                group=str(item.get("group", "")),
                severity=str(item.get("severity", "")),
                explanation=str(item.get("explanation", "")),
                source="agent",
                note=f"across {_refs(prs)}",
            )
        )
    return out


# --- the stack walkthrough ----------------------------------------------------------------------

Locate = Callable[[int, str, int], bool]


def _hunk(item: object, members: Collection[int], locate: Locate) -> bool:
    """Whether a cited ``{pr, path, hunk}`` is a hunk of a stack PR's diff."""
    if not isinstance(item, Mapping):
        return False
    pr, path, hunk = _int(item.get("pr")), item.get("path"), _int(item.get("hunk"))
    if pr is None or pr not in members or not isinstance(path, str) or hunk is None:
        return False
    return locate(pr, path, hunk)


def _list(output: Mapping[str, object], key: str) -> list[object]:
    value = output.get(key)
    return value if isinstance(value, list) else []


def known_stack_walkthrough(
    output: Mapping[str, object], members: Collection[int], locate: Locate
) -> dict[str, object]:
    """The stack walkthrough with only what Jesinia knows (as ``_known_steps`` does for one
    PR): steps whose every hunk is one of a stack PR's diff, skips of stack PRs, and cross-PR
    findings naming two or more stack PRs, a known group and severity, and known hunks."""
    steps = [
        step
        for step in _list(output, "steps")
        if isinstance(step, Mapping)
        and isinstance(step.get("hunks"), list)
        and step["hunks"]
        and all(_hunk(h, members, locate) for h in step["hunks"])
    ]
    skipped = [
        skip
        for skip in _list(output, "skipped")
        if isinstance(skip, Mapping) and _int(skip.get("pr")) in members
    ]
    cross = []
    for item in _list(output, "cross_findings"):
        if not isinstance(item, Mapping):
            continue
        prs = item.get("prs")
        named = {n for n in prs if _int(n) is not None} if isinstance(prs, list) else set()
        hunks = item.get("hunks")
        if (
            len(named) >= 2
            and named <= set(members)
            and item.get("group") in _GROUPS
            and item.get("severity") in _SEVERITIES
            and isinstance(hunks, list)
            and all(_hunk(h, members, locate) for h in hunks)
        ):
            cross.append(item)
    return {**output, "steps": steps, "skipped": skipped, "cross_findings": cross}


# --- Post all -----------------------------------------------------------------------------------

PostState = Literal["posted", "already_posted", "failed", "not_attempted"]


@dataclass(frozen=True)
class PostAllItem:
    """One PR's part of **Post all**: the round and event the author confirmed, the digest of
    the preview shown and the warnings confirmed for it."""

    number: int
    round_id: int
    event: ReviewEvent
    digest: str
    confirm: tuple[str, ...]


@dataclass(frozen=True)
class PostOutcome:
    """What **Post all** did for one PR: ``posted`` (now, or by an earlier attempt of the
    same request), ``already_posted`` (the round was posted another way: nothing sent),
    ``failed`` (with the cause and its typed code), or ``not_attempted`` (the run stopped)."""

    number: int
    round_id: int
    event: ReviewEvent
    state: PostState
    review_url: str | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PostAllResult:
    outcomes: tuple[PostOutcome, ...]


class PostAllStopped(StackError):  # noqa: N818 - the error code clients see
    """**Post all** stopped at a PR that couldn't be posted. ``outcomes`` says, per PR, what
    was posted and what wasn't; ``cause`` is the failure (also ``__cause__``)."""

    def __init__(self, outcomes: Sequence[PostOutcome], cause: Exception) -> None:
        failed = next(o for o in outcomes if o.state == "failed")
        posted = [o.number for o in outcomes if o.state in ("posted", "already_posted")]
        done = f"; posted: {_refs(posted)}" if posted else "; nothing was posted"
        super().__init__(f"Post all stopped at #{failed.number}: {cause}{done}")
        self.outcomes = tuple(outcomes)
        self.cause = cause


def order_post_all(items: Sequence[PostAllItem], stack: Sequence[int]) -> list[PostAllItem]:
    """``items`` in stack order (bottom-up), the order they are posted in.

    Raises:
        InvalidPostAllError: no items, a PR outside the stack, or a PR named twice.
    """
    if not items:
        raise InvalidPostAllError("Post all names nothing to post")
    seen: set[int] = set()
    for item in items:
        if item.number not in stack:
            raise InvalidPostAllError(f"#{item.number} isn't part of this stack ({_refs(stack)})")
        if item.number in seen:
            raise InvalidPostAllError(f"#{item.number} is named twice")
        seen.add(item.number)
    return sorted(items, key=lambda item: list(stack).index(item.number))


def attempt_key(key: str, number: int, round_id: int) -> str:
    """The posting attempt key **Post all** request ``key`` uses for one PR's round: a replay
    of the request reuses each PR's attempt, so nothing is ever posted twice."""
    return f"{key}/stack/{number}@{round_id}"
