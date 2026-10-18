# SPDX-License-Identifier: AGPL-3.0-or-later
"""Epics (spec §4, §5.2, §6.1, §6.2; plan Task 8; rulings IR4, IR17, IR27). Pure: no I/O.

- **What an epic is:** an issue with the ``epic`` label **or** sub-issues (``is_epic``); an
  ``initiative`` sits above epics with its own label (``is_initiative``). When an issue has
  children but no hierarchy label, Jesinia suggests adding the label (``lacks_epic_label``).
- **What may contain what** (``can_contain``, IR17): never itself, never one of its own
  ancestors (a cycle), never another owner's issue (GitHub's sub-issue API requires the same
  owner), and never an initiative inside an epic. The answer is a ``Refusal`` or ``None``, never
  a string-or-bool. A child in another repo of the same owner is allowed with a warning,
  "won't fit one PR" (``is_cross_repo``).
- **Readiness:** an epic is ready when every *direct* open child is complete and
  ``agent-ready`` (``epic_readiness``), and says which children block it and why. A milestone
  rolls its open issues up the same way (``rolled_up_readiness``).
- **Checklist conversion** (``plan_conversion``, IR27): the agent-free plan that turns an
  epic's task list into sub-issues: items linking an issue are matched, unlinked open items are
  drafted as new issues, and the whole plan is **one batch** of ``CreateIssue`` then
  ``SetParent`` changes, approved at once. New issues carry no labels (IR4: a label is never
  created implicitly). ``new_epic_plan`` is the same with the epic itself created (a grouping
  proposal, a theme turned into an epic).
- **Proposals:** what an agent's grouping reply may draft (``group_drafts``, ``split_draft``)
  and what approving one changes (``proposal_batch``). A reply is advice: only issues Jesinia
  listed survive, and every proposal is a draft.
- **Delivery briefs:** ``compose_brief`` renders one from Jesinia's own facts (the children in
  order, their links) and the scope and criteria (the agent's, or drawn from the issues' text);
  ``brief_comment`` is how it is stored on the epic, marked ``<!-- jesinia:brief -->``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from jesinia.apps.issues.domain.changes import (
    ChangeBatch,
    CreateIssue,
    Existing,
    InvalidChangeError,
    IssueChange,
    New,
    RepoChange,
    SetParent,
    Target,
    batch_json,
    change_json,
)
from jesinia.apps.issues.domain.schema import LabelSchema, classify
from jesinia.apps.issues.domain.suggestions import (
    AGENT_READY,
    Draft,
    Suggestion,
    SuggestionStaleError,
    one_line,
)
from jesinia.apps.issues.domain.triage import Triage
from jesinia.services.github.domain.issue import (
    COMMENT_BODY_LIMIT,
    TITLE_LIMIT,
    Issue,
    IssueRef,
)

Key = tuple[str, int]
RefusalCode = Literal["self", "cycle", "cross_owner", "initiative"]
ItemKind = Literal["issue", "epic", "milestone"]
ITEM_KINDS: tuple[ItemKind, ...] = ("issue", "epic", "milestone")

CROSS_REPO_WARNING = "won't fit one PR"
BRIEF_MARKER = "<!-- jesinia:brief -->"
MAX_CHECKLIST_ITEMS = 100
SCOPE_MAX = 1500
ACCEPTANCE_MAX = 30
_NOT_CACHED = "not in the cache; sync its repo first"
_OTHER_OWNER = "another owner's issue (GitHub's sub-issues require the same owner)"
_TASK = re.compile(r"^\s{0,3}[-*+]\s+\[([ xX])\]\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_REF = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d{1,9})\b")
_FULL_REF = re.compile(r"(?<![\w/])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,9})\b")
_SHORT_REF = re.compile(r"(?<![\w/#])#(\d{1,9})\b")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")


class EpicDomainError(ValueError):
    """Base for invalid epic requests."""


class NotAnEpicError(EpicDomainError):
    """The issue is neither labelled an epic nor has sub-issues."""


class ConversionOutdatedError(EpicDomainError):
    """The epic's checklist (or what it links) changed since the plan was shown: preview it
    again before approving."""


class NothingToConvertError(EpicDomainError):
    """The plan has nothing to create or link."""


class InvalidBriefError(EpicDomainError):
    """A brief that can't be posted (blank, or over GitHub's comment limit)."""


@dataclass(frozen=True)
class Refusal:
    """Why one issue can't contain another (shown as "can't drop here")."""

    code: RefusalCode
    reason: str


class ContainmentRefusedError(InvalidChangeError):
    """A parent link ``can_contain`` refuses; ``refusal`` says why. Nothing is staged."""

    def __init__(self, refusal: Refusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.reason)


def issue_key(repo: str, n: int) -> Key:
    return (repo.casefold(), n)


def _owner(repo: str) -> str:
    return repo.split("/", 1)[0].casefold()


def _name(key: Key | tuple[str, int]) -> str:
    return f"{key[0]}#{key[1]}"


# What an epic is ------------------------------------------------------------------------------


def is_epic(issue: Issue, schema: LabelSchema) -> bool:
    """The ``epic`` label (or an alias) or sub-issues (spec §6.2)."""
    has_children = issue.sub_issue_count > 0 or bool(issue.sub_issues)
    return has_children or classify(issue.labels, schema).is_epic


def is_initiative(issue: Issue, schema: LabelSchema) -> bool:
    """The ``initiative`` label (or an alias): it sits above epics."""
    return classify(issue.labels, schema).is_initiative


def lacks_epic_label(issue: Issue, schema: LabelSchema) -> bool:
    """It has children but neither hierarchy label: Jesinia suggests adding ``epic``."""
    classified = classify(issue.labels, schema)
    has_children = issue.sub_issue_count > 0 or bool(issue.sub_issues)
    return has_children and not (classified.is_epic or classified.is_initiative)


# Containment (IR17) --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """An issue as the containment rules see it."""

    repo: str
    n: int
    is_initiative: bool = False

    @property
    def key(self) -> Key:
        return issue_key(self.repo, self.n)


def can_contain(parent: Node, child: Node, ancestors: Sequence[Key]) -> Refusal | None:
    """Whether ``parent`` may take ``child`` as a sub-issue; ``ancestors`` are the parent's own
    ancestors (its parent, that one's parent, and so on). ``None``: it may (see
    ``is_cross_repo`` for the warning)."""
    if child.key == parent.key:
        return Refusal("self", "an issue can't contain itself")
    if _owner(child.repo) != _owner(parent.repo):
        return Refusal("cross_owner", _OTHER_OWNER)
    if child.key in set(ancestors):
        return Refusal("cycle", "that would make a cycle: it is already above this epic")
    if child.is_initiative and not parent.is_initiative:
        return Refusal("initiative", "an initiative sits above epics; it can't go inside one")
    return None


def is_cross_repo(parent: Node, child: Node) -> bool:
    """A child in another repo (of the same owner): allowed, but it "won't fit one PR"."""
    return parent.repo.casefold() != child.repo.casefold()


# Readiness -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    """A child of an epic (or an issue of a milestone): ``triage`` is ``None`` when it isn't
    cached (a child in a repo nobody added)."""

    repo: str
    n: int
    title: str
    is_open: bool
    triage: Triage | None


@dataclass(frozen=True)
class Blocker:
    repo: str
    n: int
    reason: str


@dataclass(frozen=True)
class Readiness:
    """Whether an item is ready to hand to an agent. ``blockers`` name each issue in the way and
    why; ``cross_repo`` warns that an epic's children span repos (``warning``); ``note`` says why
    an item with nothing to check isn't ready."""

    ready: bool
    blockers: tuple[Blocker, ...]
    cross_repo: bool
    note: str | None = None

    @property
    def warning(self) -> str | None:
        return CROSS_REPO_WARNING if self.cross_repo else None


def member_blocker(member: Member) -> str | None:
    """Why an open issue isn't ready (complete and ``agent-ready``), or ``None``."""
    found = member.triage
    if found is None:
        return _NOT_CACHED
    if found.conflicts:
        return f"conflicting labels: {'; '.join(found.conflicts)}"
    if found.missing:
        return f"missing {', '.join(found.missing)}"
    if found.readiness != AGENT_READY:
        return f"readiness is {found.readiness or 'unset'}, not {AGENT_READY}"
    return None


def _blockers(members: Iterable[Member]) -> tuple[Blocker, ...]:
    found = ((m, member_blocker(m)) for m in members if m.is_open)
    return tuple(Blocker(m.repo, m.n, reason) for m, reason in found if reason is not None)


def issue_readiness(member: Member) -> Readiness:
    """A single issue: ready when open, complete and ``agent-ready``."""
    if not member.is_open:
        return Readiness(False, (), False, "it is closed")
    blockers = _blockers([member])
    return Readiness(not blockers, blockers, False)


def epic_readiness(epic: Member, children: Sequence[Member] | None) -> Readiness:
    """Ready when every *direct* open child is ready (spec §6.2); a closed child never blocks.
    ``children`` is ``None`` while they were never fetched."""
    if children is None:
        return Readiness(False, (), False, "its children haven't been fetched yet; refresh it")
    crossing = any(child.repo.casefold() != epic.repo.casefold() for child in children)
    if not children:
        return Readiness(False, (), False, "it has no children yet")
    if not any(child.is_open for child in children):
        return Readiness(False, (), crossing, "every child is closed")
    blockers = _blockers(children)
    return Readiness(not blockers, blockers, crossing)


def rolled_up_readiness(members: Sequence[Member]) -> Readiness:
    """A milestone: ready when every open issue is ready."""
    if not any(member.is_open for member in members):
        return Readiness(False, (), False, "it has no open issues")
    blockers = _blockers(members)
    return Readiness(not blockers, blockers, False)


# Checklists ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChecklistItem:
    """A task-list item of an epic's body: its text, whether it is checked, and the issue it
    links (``(repo, n)`` as written; ``#n`` means the epic's repo)."""

    text: str
    checked: bool
    ref: tuple[str, int] | None


def _first_ref(text: str, repo: str) -> tuple[str, int] | None:
    found: list[tuple[int, tuple[str, int]]] = []
    for pattern in (_URL_REF, _FULL_REF):
        match = pattern.search(text)
        if match:
            found.append((match.start(), (match.group(1), int(match.group(2)))))
    short = _SHORT_REF.search(text)
    if short:
        found.append((short.start(), (repo, int(short.group(1)))))
    return min(found)[1] if found else None


def checklist(body: str, repo: str) -> list[ChecklistItem]:
    """The task-list items of ``body`` outside fenced code blocks, in order."""
    items: list[ChecklistItem] = []
    fence: str | None = None
    for line in body.splitlines():
        opened = _FENCE.match(line)
        if opened:
            marker = opened.group(1)
            fence = marker if fence is None else (None if marker == fence else fence)
            continue
        task = None if fence is not None else _TASK.match(line)
        if task is None:
            continue
        text = " ".join(_LINK.sub(r"\1", task.group(2)).split())
        items.append(ChecklistItem(text, task.group(1) != " ", _first_ref(task.group(2), repo)))
    return items


# Conversion plans (IR27) ---------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """An issue as a plan or an approval checks it: ``repo`` as GitHub spells it, whether it is
    open, its parent (``None`` for none) and whether it is an initiative."""

    repo: str
    n: int
    is_open: bool
    parent: tuple[str, int] | None
    is_initiative: bool

    @property
    def key(self) -> Key:
        return issue_key(self.repo, self.n)

    @property
    def node(self) -> Node:
        return Node(self.repo, self.n, self.is_initiative)


@dataclass(frozen=True)
class PlannedChild:
    """A child the plan links: an existing issue, or one it creates (``is_new``)."""

    item: str
    target: Target
    title: str
    is_new: bool
    warning: str | None = None


@dataclass(frozen=True)
class SkippedItem:
    item: str
    reason: str


@dataclass(frozen=True)
class ConversionPlan:
    """Turning a checklist (or a group of issues) into sub-issues: the ``epic`` (an existing
    issue, or one the batch creates), the ``children`` in order, what was skipped and why, and
    ``changes``, the one batch an approval applies."""

    epic: CreateIssue | IssueRef
    children: tuple[PlannedChild, ...]
    changes: ChangeBatch
    skipped: tuple[SkippedItem, ...] = field(default=())

    @property
    def digest(self) -> str:
        """What the plan would do, as a hash: an approval names the plan it saw."""
        epic: object = (
            change_json(self.epic)
            if isinstance(self.epic, CreateIssue)
            else [self.epic.repo.casefold(), self.epic.number]
        )
        text = json.dumps([epic, batch_json(self.changes)], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _title(text: str) -> str:
    return text[:TITLE_LIMIT]


def _link(parent: Target) -> SetParent:
    # Linked against "no parent": a parent set on GitHub since is a conflict, never a move.
    return SetParent(parent, expects_parent=True, expected_parent=None)


def _plan(
    epic: CreateIssue | IssueRef,
    parent: Target,
    children: Sequence[PlannedChild],
    created: Sequence[RepoChange],
    skipped: Sequence[SkippedItem],
) -> ConversionPlan:
    issues = {child.target: [cast(IssueChange, _link(parent))] for child in children}
    return ConversionPlan(epic, tuple(children), ChangeBatch.of(issues, created), tuple(skipped))


def _linkable(
    parent: Node, ancestors: Sequence[Key], found: Facts | None, seen: set[Key], key: Key
) -> str | None:
    """Why an existing issue can't become the parent's child now, or ``None``. A closed issue
    may (a checked-off item's issue is a done child); a proposal refuses closed ones itself."""
    if found is None:
        return _NOT_CACHED
    if key in seen:
        return "listed twice"
    refusal = can_contain(parent, found.node, ancestors)
    if refusal is not None:
        return refusal.reason
    if found.parent is not None:
        return f"already under {_name(found.parent)}"
    return None


def plan_conversion(
    epic: Facts,
    ancestors: Sequence[Key],
    items: Sequence[ChecklistItem],
    known: Mapping[Key, Facts],
    children: set[Key],
) -> ConversionPlan:
    """The plan converting ``epic``'s checklist ``items``. ``ancestors`` are the epic's own,
    ``known`` the cached issues the items may link, ``children`` its sub-issues now. A linked
    item becomes a child (a closed one too: it counts as done) unless it is uncached, already a
    child, listed twice, refused by ``can_contain`` or under another parent; an unchecked
    unlinked item becomes a new issue in the epic's repo (no labels, IR4); a checked unlinked
    one is done already."""
    parent = epic.node
    planned: list[PlannedChild] = []
    created: list[RepoChange] = []
    skipped: list[SkippedItem] = []
    seen: set[Key] = set()
    for index, item in enumerate(items, start=1):
        if index > MAX_CHECKLIST_ITEMS:
            skipped.append(SkippedItem(item.text, f"over the {MAX_CHECKLIST_ITEMS}-item limit"))
            continue
        if item.ref is None:
            if item.checked:
                done = "checked off with no linked issue: nothing to create"
                skipped.append(SkippedItem(item.text, done))
                continue
            temp = f"item-{index}"
            body = f"Part of {epic.repo}#{epic.n}, from its checklist."
            created.append(CreateIssue(temp, epic.repo, _title(item.text), body))
            planned.append(PlannedChild(item.text, New(temp), _title(item.text), True))
            continue
        repo, n = item.ref
        key = issue_key(repo, n)
        if _owner(repo) != _owner(epic.repo):
            skipped.append(SkippedItem(item.text, _OTHER_OWNER))
            continue
        if key in children:
            skipped.append(SkippedItem(item.text, "already a sub-issue"))
            continue
        found = known.get(key)
        reason = _linkable(parent, ancestors, found, seen, key)
        if reason is not None or found is None:
            skipped.append(SkippedItem(item.text, reason or _NOT_CACHED))
            continue
        seen.add(key)
        warning = CROSS_REPO_WARNING if is_cross_repo(parent, found.node) else None
        planned.append(
            PlannedChild(item.text, Existing(found.repo, found.n), item.text, False, warning)
        )
    return _plan(
        IssueRef(epic.repo, epic.n), Existing(epic.repo, epic.n), planned, created, skipped
    )


def new_epic_plan(
    repo: str, title: str, body: str, members: Sequence[Facts], temp_id: str = "epic"
) -> ConversionPlan:
    """A plan creating the epic itself (in ``repo``, with no labels: its children make it an
    epic) and linking ``members`` under it, each checked as ``plan_conversion`` checks links."""
    create = CreateIssue(temp_id, repo, _title(title), body)
    parent = Node(repo, 0)
    planned: list[PlannedChild] = []
    skipped: list[SkippedItem] = []
    seen: set[Key] = set()
    for found in members:
        name = f"{found.repo}#{found.n}"
        reason = _linkable(parent, (), found, seen, found.key)
        if reason is not None:
            skipped.append(SkippedItem(name, reason))
            continue
        seen.add(found.key)
        warning = CROSS_REPO_WARNING if is_cross_repo(parent, found.node) else None
        planned.append(PlannedChild(name, Existing(found.repo, found.n), name, False, warning))
    return _plan(create, New(temp_id), planned, [create], skipped)


# Proposals -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupProposal:
    title: str
    scope: str
    issues: tuple[tuple[str, int], ...]
    reason: str


@dataclass(frozen=True)
class AdditionProposal:
    epic: tuple[str, int]
    issue: tuple[str, int]
    reason: str


@dataclass(frozen=True)
class SplitProposal:
    issue: tuple[str, int]
    parts: tuple[tuple[str, str], ...]
    reason: str


def _pair(data: Mapping[str, object]) -> tuple[str, int]:
    return (str(data["repo"]), cast(int, data["number"]))


@dataclass(frozen=True)
class GroupingReply:
    """A reply already valid against ``schemas/grouping.json``."""

    groups: tuple[GroupProposal, ...]
    additions: tuple[AdditionProposal, ...]
    splits: tuple[SplitProposal, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> GroupingReply:
        groups = cast(list[dict[str, object]], data["groups"])
        additions = cast(list[dict[str, dict[str, object] | str]], data["additions"])
        splits = cast(list[dict[str, object]], data["splits"])
        return cls(
            tuple(
                GroupProposal(
                    str(g["title"]), str(g["scope"]),
                    tuple(_pair(i) for i in cast(list[dict[str, object]], g["issues"])),
                    str(g["reason"]),
                )
                for g in groups
            ),
            tuple(
                AdditionProposal(
                    _pair(cast(dict[str, object], a["epic"])),
                    _pair(cast(dict[str, object], a["issue"])),
                    str(a["reason"]),
                )
                for a in additions
            ),
            tuple(
                SplitProposal(
                    _pair(cast(dict[str, object], s["issue"])),
                    tuple(
                        (str(p["title"]), str(p["body"]))
                        for p in cast(list[dict[str, object]], s["parts"])
                    ),
                    str(s["reason"]),
                )
                for s in splits
            ),
        )  # fmt: skip


def _ref_json(found: Facts) -> dict[str, object]:
    return {"repo": found.repo, "n": found.n}


def group_drafts(
    reply: GroupingReply, candidates: Mapping[Key, Facts], epics: Mapping[Key, Facts]
) -> list[Draft]:
    """The drafts a grouping reply may produce: a new group of at least two of the listed
    ``candidates`` (open, ungrouped issues), all under the first one's owner; an addition of a
    candidate to one of the listed ``epics`` that ``can_contain`` allows. Repeats, unknown
    issues and blank reasons are dropped (splits are ``split_draft``'s)."""
    drafts: list[Draft] = []
    for group in reply.groups:
        reason = one_line(group.reason)
        found: list[Facts] = []
        for repo, n in group.issues:
            member = candidates.get(issue_key(repo, n))
            if member is None or member in found:
                continue
            if found and _owner(member.repo) != _owner(found[0].repo):
                continue
            found.append(member)
        if len(found) < 2 or not reason:
            continue
        home = found[0].repo
        payload: dict[str, object] = {
            "repo": home,
            "title": _title(one_line(group.title, TITLE_LIMIT)),
            "scope": group.scope.strip(),
            "issues": [_ref_json(m) for m in found],
            "cross_repo": any(m.repo.casefold() != home.casefold() for m in found),
        }
        drafts.append(Draft("group", payload, reason))
    for addition in reply.additions:
        reason = one_line(addition.reason)
        epic = epics.get(issue_key(*addition.epic))
        member = candidates.get(issue_key(*addition.issue))
        if epic is None or member is None or not reason:
            continue
        if can_contain(epic.node, member.node, ()) is not None:
            continue
        payload = {"epic": _ref_json(epic), "issues": [_ref_json(member)]}
        drafts.append(Draft("group", payload, reason))
    return drafts


def split_draft(reply: GroupingReply, asked: Facts) -> Draft | None:
    """The split of the asked issue in ``reply`` (the first one about it), or ``None``."""
    for split in reply.splits:
        reason = one_line(split.reason)
        if issue_key(*split.issue) != asked.key or not reason:
            continue
        parts = [{"title": _title(one_line(t, TITLE_LIMIT)), "body": b} for t, b in split.parts]
        return Draft("split", {"issue": _ref_json(asked), "parts": parts}, reason)
    return None


def groups_scope(repo: str | None) -> str:
    """The ``scope`` of the group drafts asked for one repo (or every watched repo)."""
    return f"groups:{'*' if repo is None else repo.casefold()}"


def split_scope(repo: str, n: int) -> str:
    return f"split:{repo.casefold()}#{n}"


def brief_scope(repo: str, n: int) -> str:
    return f"brief:{repo.casefold()}#{n}"


def _facts(known: Mapping[Key, Facts], data: object) -> Facts:
    item = cast(dict[str, object], data)
    repo, n = str(item["repo"]), cast(int, item["n"])
    found = known.get(issue_key(repo, n))
    if found is None:
        raise SuggestionStaleError(f"{repo}#{n} is {_NOT_CACHED}; nothing was applied")
    if not found.is_open:
        raise SuggestionStaleError(f"{repo}#{n} was closed since; nothing was applied")
    return found


def _stale(plan: ConversionPlan) -> None:
    if plan.skipped:
        reasons = "; ".join(f"{s.item}: {s.reason}" for s in plan.skipped)
        raise SuggestionStaleError(f"the proposal no longer fits ({reasons}); nothing was applied")


def proposal_batch(
    suggestion: Suggestion,
    known: Mapping[Key, Facts],
    ancestors: Mapping[Key, tuple[Key, ...]],
) -> ChangeBatch:
    """What approving a ``group`` or ``split`` draft changes, after its premise check against
    the cache (``known``; ``ancestors`` by epic):

    - a new group: create the epic in its repo, then link each issue under it;
    - an addition: link the issue under the existing epic;
    - a split: create each part in the issue's repo, then link the parts under the issue.

    Raises:
        SuggestionStaleError: an issue is uncached, closed, under a parent now, or refused by
            ``can_contain``; nothing is applied.
        InvalidChangeError: not a group or split draft, or a malformed one.
    """
    try:
        return _proposal_batch(suggestion, known, ancestors)
    except (KeyError, TypeError, AttributeError) as exc:
        raise InvalidChangeError(f"a malformed {suggestion.kind} proposal ({exc})") from exc


def _proposal_batch(
    suggestion: Suggestion,
    known: Mapping[Key, Facts],
    ancestors: Mapping[Key, tuple[Key, ...]],
) -> ChangeBatch:
    payload = suggestion.payload
    if suggestion.kind == "group" and "epic" in payload:
        epic = _facts(known, payload["epic"])
        members = [_facts(known, item) for item in cast(list[object], payload["issues"])]
        children: list[PlannedChild] = []
        skipped: list[SkippedItem] = []
        seen: set[Key] = set()
        for member in members:
            reason = _linkable(epic.node, ancestors.get(epic.key, ()), member, seen, member.key)
            if reason is not None:
                skipped.append(SkippedItem(f"{member.repo}#{member.n}", reason))
                continue
            seen.add(member.key)
            children.append(PlannedChild("", Existing(member.repo, member.n), "", False))
        plan = _plan(
            IssueRef(epic.repo, epic.n), Existing(epic.repo, epic.n), children, [], skipped
        )
        _stale(plan)
        return plan.changes
    if suggestion.kind == "group":
        members = [_facts(known, item) for item in cast(list[object], payload["issues"])]
        body = f"{str(payload['scope']).strip()}\n\nGrouped by Jesinia from an approved proposal."
        plan = new_epic_plan(str(payload["repo"]), str(payload["title"]), body.strip(), members)
        _stale(plan)
        return plan.changes
    if suggestion.kind == "split":
        whole = _facts(known, payload["issue"])
        parts = cast(list[dict[str, str]], payload["parts"])
        footer = f"Part of {whole.repo}#{whole.n}, split by Jesinia from an approved proposal."
        created: list[RepoChange] = [
            CreateIssue(
                f"part-{i}", whole.repo, p["title"], f"{p['body'].strip()}\n\n{footer}".strip()
            )
            for i, p in enumerate(parts, start=1)
        ]
        links: dict[Target, list[IssueChange]] = {
            New(f"part-{i}"): [SetParent(Existing(whole.repo, whole.n), True)]
            for i in range(1, len(parts) + 1)
        }
        return ChangeBatch.of(links, created)
    raise InvalidChangeError(f"a {suggestion.kind} suggestion isn't a proposal to apply")


# Delivery briefs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefLine:
    """A child (or a milestone's issue) as a brief lists it."""

    repo: str
    n: int
    title: str
    url: str
    is_open: bool
    readiness: str | None


@dataclass(frozen=True)
class BriefReply:
    """A reply already valid against ``schemas/brief.json``."""

    scope: str
    acceptance_criteria: tuple[str, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> BriefReply:
        criteria = (one_line(str(c)) for c in cast(list[str], data["acceptance_criteria"]))
        return cls(str(data["scope"]).strip(), tuple(c for c in criteria if c))


def default_scope(body: str) -> str:
    """The issue's prose without its task list: paragraphs joined, at most ``SCOPE_MAX``."""
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", body):
        kept = [line.strip() for line in block.splitlines() if not _TASK.match(line)]
        text = " ".join(line for line in kept if line)
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)[:SCOPE_MAX]


def derived_acceptance(bodies: Sequence[tuple[str, int, str]]) -> list[str]:
    """Acceptance criteria drawn from the children: the bullets under a heading that mentions
    acceptance, each prefixed with its issue (at most ``ACCEPTANCE_MAX``)."""
    found: list[str] = []
    for repo, n, body in bodies:
        inside = False
        for line in body.splitlines():
            heading = _HEADING.match(line)
            if heading:
                inside = "acceptance" in heading.group(1).casefold()
                continue
            bullet = _BULLET.match(line) if inside else None
            if bullet:
                found.append(f"{repo}#{n}: {one_line(bullet.group(1))}")
    return found[:ACCEPTANCE_MAX]


def compose_brief(
    title: str,
    repo: str,
    n: int,
    url: str,
    scope: str,
    lines: Sequence[BriefLine],
    acceptance: Sequence[str],
    *,
    cross_repo: bool,
) -> str:
    """The delivery brief (spec §4: one epic, one PR): scope, the children in order,
    acceptance criteria drawn from them, and links. Without the marker; ``brief_comment`` adds
    it when it is stored."""
    out = [f"# Delivery brief: {title}", "", f"{repo}#{n}, delivered as one pull request.", ""]
    if cross_repo:
        out += [f"Warning: its children span repos, so it {CROSS_REPO_WARNING}.", ""]
    out += ["## Scope", "", scope.strip() or "To be written.", "", "## Children, in order", ""]
    for index, line in enumerate(lines, start=1):
        box = "[ ]" if line.is_open else "[x]"
        state = (line.readiness or "no readiness") if line.is_open else "closed"
        out.append(f"{index}. {box} {line.repo}#{line.n} {line.title} ({state})")
    if not lines:
        out.append("No children yet.")
    out += ["", "## Acceptance criteria", ""]
    out += [f"- {criterion}" for criterion in acceptance] or ["None drawn from the children yet."]
    out += ["", "## Links", "", f"- {repo}#{n}: {url}"]
    out += [f"- {line.repo}#{line.n}: {line.url}" for line in lines]
    return "\n".join(out)


def strip_marker(text: str) -> str:
    """``text`` without any brief marker (the author's text, as shown and edited)."""
    return text.replace(BRIEF_MARKER, "").strip()


def brief_comment(text: str) -> str:
    """The comment that stores a brief: the marker on the first line, then the text (any
    marker inside it removed, so a comment carries exactly one).

    Raises:
        InvalidBriefError: blank, or over GitHub's comment limit.
    """
    body = strip_marker(text)
    if not body:
        raise InvalidBriefError("a brief needs some text")
    comment = f"{BRIEF_MARKER}\n{body}"
    if len(comment) > COMMENT_BODY_LIMIT:
        raise InvalidBriefError(f"a brief is at most {COMMENT_BODY_LIMIT} characters")
    return comment


def is_brief_comment(body: str) -> bool:
    """Whether a comment is a Jesinia brief (its first line is the marker)."""
    return body.lstrip().startswith(BRIEF_MARKER)
