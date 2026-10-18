# SPDX-License-Identifier: AGPL-3.0-or-later
"""Changes to GitHub issues, the ordered series that applies them, and their results (spec
§6.1, §6.2, §8; plan Task 6, rulings IR4, IR13–IR16). Pure: no I/O.

A **change** is what the author approved or staged: a label dimension set, an area added or
removed, a free-form label added (a published theme's ``theme:<tag>``), a parent epic linked,
a child reordered, an issue closed or commented on. Two
repo-level changes run before them: ``EnsureLabel`` (create a label, recording an approved
schema entry) and ``CreateIssue`` (open an issue that the same batch's changes may name by its
``temp_id``). Queues and themes are local services (Tasks 9 and 10), never changes (IR15).

A **batch** maps each issue to its changes. ``series`` orders it (IR13, a spec-compatible
extension of §6.2's four steps):

1. every ``EnsureLabel``, then every ``CreateIssue``, in the order given;
2. then per issue, in batch order: its **labels**, **parent**, sub-issue **order**, **close** and
   **comment** steps (each only when it has such changes).

Within one issue a later change wins over an earlier one of the same sort (one value per
dimension, one change per area, one parent, one close), so a tray holding a drag and then a
correction applies the correction. Labels are applied as an **add/remove** against a fresh read
of the issue (``label_edit``), never by replacing the whole set, and only the labels the changes
touch move. A label that doesn't exist on GitHub fails the step: Jesinia never creates a label
implicitly (``MissingLabelError``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, cast, get_args

from jesinia.apps.issues.domain.labels import create_label_op
from jesinia.apps.issues.domain.schema import (
    AREA,
    OVERLAY_DIMENSIONS,
    PRIORITY,
    READINESS,
    TYPE,
    Canonical,
    LabelSchema,
    SchemaEntry,
    label_key,
    write_name,
)
from jesinia.apps.issues.domain.suggestions import AGENT_READY, HUMAN_NEEDED, Suggestion
from jesinia.services.github.domain.issue import (
    CloseReason,
    IssueDomainError,
    validate_close,
    validate_comment_body,
    validate_issue_body,
    validate_issue_number,
    validate_issue_title,
    validate_label_color,
    validate_label_description,
    validate_label_name,
    validate_label_names,
)
from jesinia.services.github.domain.repo import InvalidRepoError, validate_repo

SingleDimension = Literal["readiness", "priority", "type"]
SINGLE_DIMENSIONS: tuple[SingleDimension, ...] = get_args(SingleDimension)
StepKind = Literal["ensure_label", "create_issue", "labels", "parent", "order", "close", "comment"]
StepState = Literal["done", "failed", "skipped"]
# A stored batch: ``running`` (written ahead of its first GitHub write, updated per step),
# ``finished`` (its result is final, complete or not), ``interrupted`` (a stop cut it off;
# Retry resumes it without repeating what is done).
BatchStatus = Literal["running", "finished", "interrupted"]
BATCH_STATUSES: tuple[BatchStatus, ...] = get_args(BatchStatus)
NOT_RUN = "not run yet"
CLOSED_SKIP = "closed on GitHub since the draft, so nothing is set on it"
# A comment or a new issue whose write was on its way when the batch was cut off: it may have
# gone out, so Retry doesn't send it blindly again (it fails once, asking to check GitHub).
IN_FLIGHT = "in flight: the write may have gone out when the batch was cut off"
UNSURE = (
    "not repeated: the batch was cut off while this was being sent, so it may have gone out; "
    "check GitHub, then retry to send it"
)
# Steps whose write GitHub would repeat (a second comment, a second issue).
UNREPEATABLE: tuple[StepKind, ...] = ("comment", "create_issue")
ALREADY_CLOSED = "already closed"
STEP_STATES: tuple[StepState, ...] = get_args(StepState)
PendingSource = Literal["drag", "picker", "menu", "bulk", "queue", "theme"]
PENDING_SOURCES: tuple[PendingSource, ...] = get_args(PendingSource)
_CLOSE_REASONS: tuple[CloseReason, ...] = get_args(CloseReason)
# The per-issue steps, in the order they run.
ISSUE_STEPS: tuple[StepKind, ...] = ("labels", "parent", "order", "close", "comment")
_TEMP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
COMMENT_JOIN = "\n\n"


class ChangeDomainError(ValueError):
    """Base for invalid changes and apply requests."""


class InvalidChangeError(ChangeDomainError):
    """A change or batch that can't be applied as written."""


class MissingLabelError(ChangeDomainError):
    """Labels a step needs don't exist on GitHub; ``names`` lists them."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)
        super().__init__(
            f"{', '.join(repr(n) for n in self.names)} "
            f"{'does' if len(self.names) == 1 else 'do'} not exist on GitHub, and Jesinia never "
            "creates labels implicitly; run the label setup (or approve the new label) first"
        )


class ChangedOnGitHubError(ChangeDomainError):
    """The fresh read contradicts what the change was approved or staged against (a dimension
    filled, a parent changed, the issue closed on GitHub since): applying it would overwrite
    someone's change, so the step fails and nothing is written (spec §6.1, §6.2)."""


class NothingPendingError(ChangeDomainError):
    """Apply was asked for, but no pending change matches."""


class PendingNotFoundError(ChangeDomainError):
    """No pending change with that id (already applied or undone)."""


class BatchNotFoundError(ChangeDomainError):
    """No applied batch with that id."""


class BatchRunningError(ChangeDomainError):
    """The batch is still running: poll it; Retry is for one that finished or was cut off."""


class EarlierBatchError(ChangeDomainError):
    """An earlier batch for the same origin (a proposal, a theme's epic, an epic's checklist
    conversion) is still running or didn't finish: approving again could create its issues a
    second time, so it is resumed only by retrying ``batch_id`` (ruling IR56)."""

    def __init__(self, batch_id: str, message: str) -> None:
        self.batch_id = batch_id
        super().__init__(message)


class PendingHeldError(ChangeDomainError):
    """A queue item's Done holds this staged change: undo it from its queue, so the item and the
    tray never disagree."""


@contextmanager
def _checked() -> Iterator[None]:
    """GitHub-value checks, reported as an invalid change."""
    try:
        yield
    except (IssueDomainError, InvalidRepoError) as exc:
        raise InvalidChangeError(str(exc)) from exc


# Targets ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Existing:
    """An issue on GitHub (``repo`` as GitHub spells it)."""

    repo: str
    n: int

    def __post_init__(self) -> None:
        with _checked():
            validate_repo(self.repo)
            validate_issue_number(self.n)

    @property
    def key(self) -> tuple[str, int]:
        return (self.repo.casefold(), self.n)

    def __str__(self) -> str:
        return f"{self.repo}#{self.n}"


@dataclass(frozen=True)
class New:
    """An issue the same batch creates (``CreateIssue.temp_id``)."""

    temp_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.temp_id, str) or not _TEMP_ID.fullmatch(self.temp_id):
            raise InvalidChangeError("a temp id is 1-64 letters, digits, '-' or '_'")

    @property
    def key(self) -> tuple[str, int]:
        return (f"new:{self.temp_id}", 0)

    def __str__(self) -> str:
        return f"the new issue {self.temp_id!r}"


Target = Existing | New


def _owner(repo: str) -> str:
    return repo.split("/", 1)[0].casefold()


# Changes ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetDimension:
    """Set a single-value dimension (readiness, priority or type) to a schema value, removing
    the dimension's other values. An approval states what it was made against, so the fresh
    read can refuse it when GitHub changed the dimension since (``ChangedOnGitHubError``):
    ``only_if_missing`` (it only promised to fill a gap) or ``only_if_value`` (the one value the
    dimension had, e.g. ``human-needed`` for a readiness gap). The value already set is fine.
    ``only_if_open`` (a priority approval, about open issues) skips an issue closed since."""

    dimension: SingleDimension
    value: str
    only_if_missing: bool = False
    only_if_value: str | None = None
    only_if_open: bool = False

    def __post_init__(self) -> None:
        if self.dimension not in SINGLE_DIMENSIONS:
            raise InvalidChangeError(f"dimension must be one of {', '.join(SINGLE_DIMENSIONS)}")
        with _checked():
            validate_label_name(self.value)
            if self.only_if_value is not None:
                validate_label_name(self.only_if_value)
        if self.only_if_missing and self.only_if_value is not None:
            raise InvalidChangeError("a change expects the dimension missing or one value")


@dataclass(frozen=True)
class AddArea:
    """Add an area: a schema area (or an alias), or any ``area:`` label."""

    value: str

    def __post_init__(self) -> None:
        with _checked():
            validate_label_name(self.value)


@dataclass(frozen=True)
class RemoveArea:
    """Remove an area (every label that means it)."""

    value: str

    def __post_init__(self) -> None:
        with _checked():
            validate_label_name(self.value)


@dataclass(frozen=True)
class AddLabel:
    """Add a free-form label: one starting with a schema ``free_prefixes`` entry (``theme:``,
    ``review:``, ``campaign:``), such as a published theme's ``theme:<tag>`` (Task 10). Like
    every label it must already exist on GitHub when it is added: a staged ``EnsureLabel``
    creates it, never the add (IR4)."""

    name: str

    def __post_init__(self) -> None:
        with _checked():
            validate_label_name(self.name)


@dataclass(frozen=True)
class SetParent:
    """Make ``parent`` the issue's epic (moving it from another), or remove its parent.

    With ``expects_parent`` the change was made against ``expected_parent`` (the parent the cache
    showed when it was staged or approved, ``None`` for none): when GitHub's parent differs at
    apply time, the step fails as a conflict and nothing moves (IR39)."""

    parent: Target | None
    expects_parent: bool = False
    expected_parent: Existing | None = None

    def __post_init__(self) -> None:
        if self.expected_parent is not None and not self.expects_parent:
            raise InvalidChangeError("an expected parent needs expects_parent")


@dataclass(frozen=True)
class ReorderChild:
    """Within the keyed epic, move ``child`` to just after ``after`` (the top when ``None``)."""

    child: Target
    after: Target | None = None


@dataclass(frozen=True)
class Close:
    """Close the issue. A duplicate carries the original's GitHub database id (sent as
    ``duplicate_issue_id``) and its reference (``duplicate_of_ref``); ``comment`` is posted
    after the close, in the comment step (and only if the close itself happened).
    ``only_if_open`` (an approval) refuses when GitHub already closed the issue."""

    reason: CloseReason
    duplicate_of_id: int | None = None
    duplicate_of_ref: Existing | None = None
    comment: str | None = None
    only_if_open: bool = False

    def __post_init__(self) -> None:
        with _checked():
            validate_close(self.reason, self.duplicate_of_id)
            if self.comment is not None:
                validate_comment_body(self.comment)
        if (self.duplicate_of_id is None) != (self.duplicate_of_ref is None):
            raise InvalidChangeError("a duplicate close names the original's id and reference")
        if self.reason == "duplicate" and self.duplicate_of_id is None:
            raise InvalidChangeError("a duplicate close names the issue it duplicates")


@dataclass(frozen=True)
class Comment:
    """A comment. ``from_close`` marks a close's own comment (made by ``series``), posted only
    when that close happened."""

    body: str
    from_close: bool = False

    def __post_init__(self) -> None:
        with _checked():
            validate_comment_body(self.body)


@dataclass(frozen=True)
class EnsureLabel:
    """Create the label on GitHub when it is missing (through the label executor, IR12) and
    record ``schema_entry``, the approved schema value, in the local overlay."""

    repo: str
    name: str
    color: str
    description: str
    schema_entry: SchemaEntry | None = None

    def __post_init__(self) -> None:
        with _checked():
            validate_repo(self.repo)
            validate_label_name(self.name)
            validate_label_color(self.color)
            validate_label_description(self.description)
        entry = self.schema_entry
        if entry is not None and (
            entry.dimension not in OVERLAY_DIMENSIONS
            or label_key(entry.canonical) != label_key(self.name)
        ):
            raise InvalidChangeError("a schema entry names the label it ensures")


@dataclass(frozen=True)
class CreateIssue:
    """Open an issue; the batch's changes may name it as ``New(temp_id)``."""

    temp_id: str
    repo: str
    title: str
    body: str
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        New(self.temp_id)
        with _checked():
            validate_repo(self.repo)
            validate_issue_title(self.title)
            validate_issue_body(self.body)
            if self.labels:
                validate_label_names(self.labels)


IssueChange = (
    SetDimension | AddArea | RemoveArea | AddLabel | SetParent | ReorderChild | Close | Comment
)
RepoChange = EnsureLabel | CreateIssue
Change = IssueChange | RepoChange
LabelChange = SetDimension | AddArea | RemoveArea | AddLabel


def _refs(change: IssueChange) -> list[Target]:
    if isinstance(change, SetParent):
        return [] if change.parent is None else [change.parent]
    if isinstance(change, ReorderChild):
        return [change.child] + ([] if change.after is None else [change.after])
    return []


@dataclass(frozen=True)
class ChangeBatch:
    """Issues and their changes, in order, plus the repo-level changes that run first.

    Raises (on construction):
        InvalidChangeError: an issue listed twice, a temp id created twice or never created, an
            issue as its own parent or neighbour, or a parent under another owner.
    """

    issues: tuple[tuple[Target, tuple[IssueChange, ...]], ...] = ()
    repo: tuple[RepoChange, ...] = ()

    def __post_init__(self) -> None:
        created: dict[str, str] = {}
        for made in self.repo:
            if isinstance(made, CreateIssue):
                if made.temp_id in created:
                    raise InvalidChangeError(f"temp id {made.temp_id!r} is created twice")
                created[made.temp_id] = made.repo
        seen: set[tuple[str, int]] = set()
        for target, changes in self.issues:
            if target.key in seen:
                raise InvalidChangeError(f"{target} is listed twice in one batch")
            seen.add(target.key)
            for ref in [target, *(r for change in changes for r in _refs(change))]:
                if isinstance(ref, New) and ref.temp_id not in created:
                    raise InvalidChangeError(f"{ref} isn't created by this batch")
            owner = _owner(self.repo_of(target))
            for change in changes:
                self._check(target, owner, change)

    def _check(self, target: Target, owner: str, change: IssueChange) -> None:
        if isinstance(change, SetParent) and change.parent is not None:
            if change.parent.key == target.key:
                raise InvalidChangeError(f"{target} can't be its own parent")
            if _owner(self.repo_of(change.parent)) != owner:
                raise InvalidChangeError(
                    "a parent epic must be under the same owner (GitHub's sub-issues require it)"
                )
        if (
            isinstance(change, Close)
            and change.duplicate_of_ref is not None
            and change.duplicate_of_ref.key == target.key
        ):
            raise InvalidChangeError(f"{target} can't be a duplicate of itself")
        if isinstance(change, ReorderChild) and (
            change.child.key == target.key
            or (change.after is not None and change.after.key == change.child.key)
        ):
            raise InvalidChangeError("a child is reordered within its epic, after another child")

    def repo_of(self, target: Target) -> str:
        """The repo an issue lives (or will live) in."""
        if isinstance(target, Existing):
            return target.repo
        return next(
            c.repo for c in self.repo if isinstance(c, CreateIssue) and c.temp_id == target.temp_id
        )

    @classmethod
    def of(
        cls,
        issues: Mapping[Target, Sequence[IssueChange]],
        repo: Sequence[RepoChange] = (),
    ) -> ChangeBatch:
        return cls(tuple((t, tuple(c)) for t, c in issues.items()), tuple(repo))


# The series ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One step of a batch: a repo-level change (``target`` is ``None`` for ``ensure_label``,
    the new issue for ``create_issue``), or one issue's changes of one sort."""

    kind: StepKind
    repo: str
    target: Target | None
    changes: tuple[Change, ...]


def _sort(change: IssueChange) -> tuple[object, ...] | None:
    """What a later change of the same sort replaces; ``None``: every one of them runs."""
    if isinstance(change, SetDimension):
        return ("dimension", change.dimension)
    if isinstance(change, AddArea | RemoveArea):
        return ("area", label_key(change.value))
    if isinstance(change, AddLabel):
        return ("label", label_key(change.name))
    if isinstance(change, SetParent):
        return ("parent",)
    if isinstance(change, Close):
        return ("close",)
    return None


def normalized(changes: Sequence[IssueChange]) -> list[IssueChange]:
    """``changes`` with each superseded by a later one of the same sort dropped."""
    kept: list[IssueChange] = []
    for change in changes:
        sort = _sort(change)
        if sort is not None:
            kept = [c for c in kept if _sort(c) != sort]
        kept.append(change)
    return kept


def _step_kind(change: IssueChange) -> StepKind:
    if isinstance(change, SetDimension | AddArea | RemoveArea | AddLabel):
        return "labels"
    if isinstance(change, SetParent):
        return "parent"
    if isinstance(change, ReorderChild):
        return "order"
    if isinstance(change, Close):
        return "close"
    return "comment"


def series(batch: ChangeBatch) -> list[Step]:
    """The batch as ordered steps (see the module docstring)."""
    steps = [
        Step("ensure_label", c.repo, None, (c,)) for c in batch.repo if isinstance(c, EnsureLabel)
    ]
    steps += [
        Step("create_issue", c.repo, New(c.temp_id), (c,))
        for c in batch.repo
        if isinstance(c, CreateIssue)
    ]
    for target, changes in batch.issues:
        groups: dict[StepKind, list[Change]] = {kind: [] for kind in ISSUE_STEPS}
        for change in normalized(changes):
            groups[_step_kind(change)].append(change)
        closes = [c for c in groups["close"] if isinstance(c, Close) and c.comment is not None]
        groups["comment"] = [
            Comment(cast(str, c.comment), from_close=True) for c in closes
        ] + groups["comment"]
        repo = batch.repo_of(target)
        steps += [
            Step(kind, repo, target, tuple(groups[kind])) for kind in ISSUE_STEPS if groups[kind]
        ]
    return steps


def comment_body(step: Step, *, with_close: bool = True) -> str:
    """The one comment a comment step posts: its bodies in order, a blank line apart; without
    the close's own comment when ``with_close`` is off (the close didn't happen)."""
    return COMMENT_JOIN.join(
        c.body for c in step.changes if isinstance(c, Comment) and (with_close or not c.from_close)
    )


# Label edits --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelEdit:
    """What a labels step asks GitHub: names to add (in one write), labels to remove (as
    GitHub spells them)."""

    add: tuple[str, ...]
    remove: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.add and not self.remove


def _matches(canonical: Canonical, label: str) -> bool:
    key = label_key(label)
    return any(label_key(name) == key for name in (canonical.name, *canonical.aliases))


def _find(values: Iterable[Canonical], value: str) -> Canonical | None:
    return next((c for c in values if _matches(c, value)), None)


def _single(schema: LabelSchema, dimension: SingleDimension) -> tuple[Canonical, ...]:
    return {READINESS: schema.readiness, PRIORITY: schema.priority, TYPE: schema.type}[dimension]


def dimension_value(schema: LabelSchema, change: SetDimension) -> Canonical:
    """The schema value ``change`` sets.

    Raises:
        InvalidChangeError: it isn't a value (or alias) of the dimension in ``schema``.
    """
    found = _find(_single(schema, change.dimension), change.value)
    if found is None:
        raise InvalidChangeError(f"{change.value!r} isn't a {change.dimension} value of this repo")
    return found


def area_value(schema: LabelSchema, value: str) -> Canonical:
    """The area ``value`` means: a schema area (aliases count), else an ``area:`` label as it is.

    Raises:
        InvalidChangeError: neither.
    """
    found = _find(schema.areas, value)
    if found is not None:
        return found
    prefix = label_key(schema.area_prefix)
    if not label_key(value).startswith(prefix) or len(label_key(value)) == len(prefix):
        raise InvalidChangeError(
            f"{value!r} isn't an area of this repo (areas start with {schema.area_prefix!r})"
        )
    return Canonical(value.strip())


def free_value(schema: LabelSchema, name: str) -> Canonical:
    """The free-form label ``name`` is: one of ``schema``'s free prefixes followed by a name.

    Raises:
        InvalidChangeError: it starts with no free-form prefix (or is only the prefix).
    """
    key = label_key(name)
    prefixes = [label_key(prefix) for prefix in schema.free_prefixes]
    if not any(key.startswith(p) and len(key) > len(p) for p in prefixes):
        raise InvalidChangeError(
            f"{name!r} isn't a free-form label of this repo (they start with "
            f"{', '.join(repr(p) for p in schema.free_prefixes)})"
        )
    return Canonical(name.strip())


def check_change(change: IssueChange, schema: LabelSchema) -> None:
    """Refuse a label change whose value ``schema`` can't place (when it is staged).

    Raises:
        InvalidChangeError: see ``dimension_value``, ``area_value`` and ``free_value``.
    """
    if isinstance(change, SetDimension):
        dimension_value(schema, change)
    elif isinstance(change, AddArea | RemoveArea):
        area_value(schema, change.value)
    elif isinstance(change, AddLabel):
        free_value(schema, change.name)


def _expect(
    change: SetDimension,
    canonical: Canonical,
    values: Sequence[Canonical],
    others: Sequence[str],
    *,
    has_target: bool,
) -> None:
    """Refuse ``change`` when the dimension on GitHub isn't what it was made against.

    Raises:
        ChangedOnGitHubError: see ``SetDimension``.
    """
    if change.only_if_missing:
        holds = not others
    elif change.only_if_value is not None:
        expected = _find(values, change.only_if_value)
        found = {_find(values, other) for other in others}
        holds = (found == {expected} and expected is not None) or (not others and has_target)
    else:
        return
    if holds:
        return
    now = ", ".join(repr(o) for o in others) or "unset"
    raise ChangedOnGitHubError(
        f"changed on GitHub since the last sync: {change.dimension} is now {now}, so "
        f"{canonical.name!r} isn't set"
    )


def label_edit(
    changes: Sequence[LabelChange],
    current: Sequence[str],
    repo_labels: Sequence[str],
    schema: LabelSchema,
) -> LabelEdit:
    """The add/remove that applies ``changes`` to an issue whose labels are ``current`` now, on
    a repo whose labels are ``repo_labels``. Labels the changes don't touch never move; a label
    already there isn't added again, one already gone isn't removed. Until a repo is migrated an
    alias that exists on GitHub is written (``write_name``).

    Raises:
        InvalidChangeError: a value outside the schema.
        ChangedOnGitHubError: a dimension GitHub changed since the change was made (see
            ``SetDimension``).
        MissingLabelError: labels to add that GitHub doesn't have (every one of them listed).
    """
    present = {label_key(label): label.strip() for label in current if label.strip()}
    original = dict(present)
    missing: list[str] = []

    def add(canonical: Canonical) -> None:
        if any(_matches(canonical, label) for label in present.values()):
            return
        name = write_name(canonical, repo_labels)
        if name is None:
            missing.append(canonical.name)
        else:
            present[label_key(name)] = name

    def drop(keep: Callable[[str], bool]) -> None:
        for key in [k for k, label in present.items() if not keep(label)]:
            del present[key]

    for change in changes:
        if isinstance(change, SetDimension):
            canonical = dimension_value(schema, change)
            values = _single(schema, change.dimension)
            others = [p for p in present.values() if (_find(values, p) or canonical) != canonical]
            has_target = any(_matches(canonical, p) for p in present.values())
            _expect(change, canonical, values, others, has_target=has_target)
            drop(lambda label: (_find(values, label) or canonical) == canonical)
            add(canonical)
        elif isinstance(change, AddArea):
            add(area_value(schema, change.value))
        elif isinstance(change, AddLabel):
            add(free_value(schema, change.name))
        else:
            area = area_value(schema, change.value)
            drop(lambda label: not _matches(area, label))
    if missing:
        raise MissingLabelError(missing)
    return LabelEdit(
        add=tuple(label for key, label in present.items() if key not in original),
        remove=tuple(label for key, label in original.items() if key not in present),
    )


def labels_needed(changes: Sequence[Change], schema: LabelSchema) -> set[str]:
    """The labels (as ``label_key``) a step's changes may add: what a failed ``EnsureLabel``
    would leave them without."""
    needed: set[str] = set()
    for change in changes:
        if isinstance(change, AddLabel):
            needed.add(label_key(change.name))
        elif isinstance(change, AddArea | SetDimension):
            needed.add(label_key(change.value))
            try:
                if isinstance(change, AddArea):
                    canonical = area_value(schema, change.value)
                else:
                    canonical = dimension_value(schema, change)
            except InvalidChangeError:
                continue  # the step itself reports it
            needed.update(label_key(name) for name in (canonical.name, *canonical.aliases))
    return needed


# Results ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """A step's outcome; ``cause`` says why it failed or was skipped (or a no-op's reason).
    ``is_conflict``: it failed because GitHub changed underneath it (``ChangedOnGitHubError``)."""

    step: Step
    state: StepState
    cause: str | None = None
    is_conflict: bool = False


@dataclass(frozen=True)
class IssueResult:
    target: Target
    steps: tuple[StepResult, ...]


@dataclass(frozen=True)
class CreatedIssue:
    """An issue a ``CreateIssue`` step opened: its number and GitHub database id."""

    repo: str
    n: int
    id: int


@dataclass(frozen=True)
class BatchResult:
    """A batch's outcome, step by step in ``series`` order. ``created`` maps temp ids to the
    issues opened; ``stopped`` is the cause when GitHub stopped the batch (a rate limit, with
    ``rate_limited_until``, or a rejected token)."""

    results: tuple[StepResult, ...]
    created: Mapping[str, CreatedIssue] = field(default_factory=dict)
    stopped: str | None = None
    is_rate_limited: bool = False
    rate_limited_until: datetime | None = None

    @property
    def complete(self) -> bool:
        return all(result.state == "done" for result in self.results)

    @property
    def repo_steps(self) -> tuple[StepResult, ...]:
        return tuple(r for r in self.results if r.step.kind not in ISSUE_STEPS)

    @property
    def per_issue(self) -> dict[Target, IssueResult]:
        grouped: dict[Target, list[StepResult]] = {}
        for result in self.results:
            if result.step.kind in ISSUE_STEPS and result.step.target is not None:
                grouped.setdefault(result.step.target, []).append(result)
        return {target: IssueResult(target, tuple(steps)) for target, steps in grouped.items()}

    @classmethod
    def not_run(cls, batch: ChangeBatch) -> BatchResult:
        """A batch's result before it runs: every step ``skipped``, ``not run yet``."""
        return cls(tuple(StepResult(step, "skipped", NOT_RUN) for step in series(batch)))

    def first_failure(self) -> str | None:
        """The first failed step's cause (what a suggestion records when its apply failed)."""
        return next((r.cause for r in self.results if r.state == "failed"), None)


# Pending changes ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingChange:
    """A change staged in the tray (by a drag, a picker, a menu, the bulk bar, a queue or a
    theme): a change to the issue ``target``, or (``target`` ``None``) a repo's label setup step,
    an ``EnsureLabel`` (a published theme's label, Task 10), which runs before every issue's
    steps when the tray is applied.

    Raises (on construction):
        InvalidChangeError: an issue change without its issue, or a label setup step with one.
    """

    id: str
    target: Existing | None
    change: IssueChange | EnsureLabel
    created_at: datetime
    source: PendingSource

    def __post_init__(self) -> None:
        if (self.target is None) != isinstance(self.change, EnsureLabel):
            raise InvalidChangeError(
                "a staged change names its issue; a staged label setup step names none"
            )

    @property
    def repo(self) -> str:
        if isinstance(self.change, EnsureLabel):
            return self.change.repo
        return cast(Existing, self.target).repo

    @property
    def key(self) -> tuple[str, int] | None:
        """The issue's key, or ``None`` for a label setup step."""
        return None if self.target is None else self.target.key


def parse_source(value: str) -> PendingSource:
    for source in PENDING_SOURCES:
        if value == source:
            return source
    raise InvalidChangeError(f"source must be one of {', '.join(PENDING_SOURCES)}")


def pending_batch(changes: Sequence[PendingChange]) -> ChangeBatch:
    """The staged changes as one batch: the label setup steps first (each label once), then per
    issue, in the order staged."""
    grouped: dict[tuple[str, int], tuple[Existing, list[IssueChange]]] = {}
    ensured: dict[tuple[str, str], EnsureLabel] = {}
    for staged in changes:
        change = staged.change
        if isinstance(change, EnsureLabel):
            ensured.setdefault((change.repo.casefold(), label_key(change.name)), change)
            continue
        target = cast(Existing, staged.target)
        grouped.setdefault(target.key, (target, []))[1].append(change)
    return ChangeBatch(
        tuple((target, tuple(found)) for target, found in grouped.values()),
        tuple(ensured.values()),
    )


# Approvals ----------------------------------------------------------------------------------


def approval_batch(suggestion: Suggestion, duplicate_of_id: int | None = None) -> ChangeBatch:
    """The changes approving ``suggestion`` makes (spec §6.2: an approval applies at once):

    - ``label``: set the dimension, or add the area;
    - ``new_area``: ensure the label (recording it in the schema overlay), then add it;
    - ``duplicate``: close as a duplicate of the original (``duplicate_of_id`` is its database
      id) with a comment linking it;
    - ``parent``: link the epic;
    - ``readiness_gap``: ``ready: true`` sets readiness to ``agent-ready``; ``ready: false`` is
      advice only, and changes nothing.

    Raises:
        InvalidChangeError: not a per-issue suggestion, or a duplicate without its id.
    """
    if suggestion.kind == "priority":
        return _priority_batch(suggestion)
    if suggestion.repo is None or suggestion.n is None:
        raise InvalidChangeError(f"a {suggestion.kind} suggestion can't be approved yet")
    target = Existing(suggestion.repo, suggestion.n)
    payload = suggestion.payload
    kind = suggestion.kind
    if kind in ("label", "new_area"):
        value = str(payload["value"])
        if payload["dimension"] != AREA:
            dimension = cast(SingleDimension, payload["dimension"])
            fill = SetDimension(dimension, value, only_if_missing=True)
            return ChangeBatch.of({target: [fill]})
        repo: list[RepoChange] = []
        if kind == "new_area":
            op = create_label_op(AREA, Canonical(value))
            entry = SchemaEntry("area", value)
            repo.append(EnsureLabel(target.repo, op.name, op.color, op.description, entry))
        return ChangeBatch.of({target: [AddArea(value)]}, repo)
    if kind in ("duplicate", "parent"):
        ref = Existing(str(payload["repo"]), cast(int, payload["n"]))
        if kind == "parent":
            return ChangeBatch.of({target: [SetParent(ref, expects_parent=True)]})
        if duplicate_of_id is None:
            raise InvalidChangeError("a duplicate close needs the original's database id")
        close = Close("duplicate", duplicate_of_id, ref, f"Duplicate of {ref}", only_if_open=True)
        return ChangeBatch.of({target: [close]})
    if kind == "readiness_gap" and payload["ready"]:
        # The premise is the readiness it was made against (human-needed), not a gap.
        ready = SetDimension("readiness", AGENT_READY, only_if_value=HUMAN_NEEDED)
        return ChangeBatch.of({target: [ready]})
    if kind == "readiness_gap":
        return ChangeBatch()
    raise InvalidChangeError(f"a {kind} suggestion can't be approved yet")


def _priority_batch(suggestion: Suggestion) -> ChangeBatch:
    """Set each item's priority, as the draft was made against the label it had: filling a gap
    only while it is still missing, replacing a value only while it is still that one, and only
    while the issue is still open. An item
    without a value to set, already at it, or applied by an earlier approval (its outcome),
    changes nothing."""
    changes: dict[Target, list[IssueChange]] = {}
    for item in cast(list[dict[str, object]], suggestion.payload["items"]):
        value, current = item["priority"], item["current"]
        outcome = cast(dict[str, object] | None, item.get("outcome"))
        if value is None or value == current or (outcome and outcome["state"] == "applied"):
            continue
        target = Existing(str(item["repo"]), cast(int, item["n"]))
        guard = (
            SetDimension("priority", str(value), only_if_missing=True, only_if_open=True)
            if current is None
            else SetDimension("priority", str(value), only_if_value=str(current), only_if_open=True)
        )
        changes[target] = [guard]
    return ChangeBatch.of(changes)


# JSON ---------------------------------------------------------------------------------------

Json = dict[str, object]


def target_json(target: Target) -> Json:
    if isinstance(target, Existing):
        return {"repo": target.repo, "n": target.n}
    return {"temp_id": target.temp_id}


def _target(data: object) -> Target:
    if not isinstance(data, dict):
        raise InvalidChangeError("an issue is {repo, n} or {temp_id}")
    if set(data) == {"temp_id"}:
        return New(data["temp_id"])
    if set(data) == {"repo", "n"} and isinstance(data["repo"], str) and _is_int(data["n"]):
        return Existing(data["repo"], data["n"])
    raise InvalidChangeError("an issue is {repo, n} or {temp_id}")


def _optional_target(data: object) -> Target | None:
    return None if data is None else _target(data)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def change_json(change: Change) -> Json:
    """``change`` as stored (and as ``parse_change`` reads it back)."""
    if isinstance(change, SetDimension):
        return {
            "kind": "set_dimension",
            "dimension": change.dimension,
            "value": change.value,
            "only_if_missing": change.only_if_missing,
            "only_if_value": change.only_if_value,
            "only_if_open": change.only_if_open,
        }
    if isinstance(change, AddArea | RemoveArea):
        kind = "add_area" if isinstance(change, AddArea) else "remove_area"
        return {"kind": kind, "value": change.value}
    if isinstance(change, AddLabel):
        return {"kind": "add_label", "name": change.name}
    if isinstance(change, SetParent):
        parent = None if change.parent is None else target_json(change.parent)
        expected = change.expected_parent
        return {
            "kind": "set_parent",
            "parent": parent,
            "expects_parent": change.expects_parent,
            "expected_parent": None if expected is None else target_json(expected),
        }
    if isinstance(change, ReorderChild):
        after = None if change.after is None else target_json(change.after)
        return {"kind": "reorder_child", "child": target_json(change.child), "after": after}
    if isinstance(change, Close):
        ref = change.duplicate_of_ref
        return {
            "kind": "close",
            "reason": change.reason,
            "duplicate_of_id": change.duplicate_of_id,
            "duplicate_of_ref": None if ref is None else target_json(ref),
            "comment": change.comment,
            "only_if_open": change.only_if_open,
        }
    if isinstance(change, Comment):
        return {"kind": "comment", "body": change.body}
    if isinstance(change, EnsureLabel):
        entry = change.schema_entry
        return {
            "kind": "ensure_label",
            "repo": change.repo,
            "name": change.name,
            "color": change.color,
            "description": change.description,
            "schema_entry": None
            if entry is None
            else {
                "dimension": entry.dimension,
                "canonical": entry.canonical,
                "aliases": list(entry.aliases),
            },
        }
    return {
        "kind": "create_issue",
        "temp_id": change.temp_id,
        "repo": change.repo,
        "title": change.title,
        "body": change.body,
        "labels": list(change.labels),
    }


def _text(data: Mapping[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise InvalidChangeError(f"{key} must be text")
    return value


def _optional_text(data: Mapping[str, object], key: str) -> str | None:
    return None if data.get(key) is None else _text(data, key)


def _entry(data: object) -> SchemaEntry | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise InvalidChangeError("a schema entry is {dimension, canonical, aliases}")
    dimension = next((d for d in OVERLAY_DIMENSIONS if d == data["dimension"]), None)
    if dimension is None:
        raise InvalidChangeError(f"dimension must be one of {', '.join(OVERLAY_DIMENSIONS)}")
    aliases = data["aliases"]
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise InvalidChangeError("aliases must be a list of labels")
    return SchemaEntry(dimension, _text(data, "canonical"), tuple(aliases))


def _flag(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise InvalidChangeError(f"{key} must be true or false")
    return value


def _reason(value: object) -> CloseReason:
    found = next((r for r in _CLOSE_REASONS if r == value), None)
    if found is None:
        raise InvalidChangeError(f"reason must be one of {', '.join(_CLOSE_REASONS)}")
    return found


def _dimension(value: object) -> SingleDimension:
    found = next((d for d in SINGLE_DIMENSIONS if d == value), None)
    if found is None:
        raise InvalidChangeError(f"dimension must be one of {', '.join(SINGLE_DIMENSIONS)}")
    return found


def _parse(data: Mapping[str, object]) -> Change:
    kind = data["kind"]
    if kind == "set_dimension":
        return SetDimension(
            _dimension(data["dimension"]),
            _text(data, "value"),
            _flag(data, "only_if_missing"),
            _optional_text(data, "only_if_value"),
            _flag(data, "only_if_open"),
        )
    if kind == "add_area":
        return AddArea(_text(data, "value"))
    if kind == "remove_area":
        return RemoveArea(_text(data, "value"))
    if kind == "add_label":
        return AddLabel(_text(data, "name"))
    if kind == "set_parent":
        expected = _optional_target(data.get("expected_parent"))
        if expected is not None and not isinstance(expected, Existing):
            raise InvalidChangeError("an expected parent is an existing issue")
        return SetParent(_optional_target(data["parent"]), _flag(data, "expects_parent"), expected)
    if kind == "reorder_child":
        return ReorderChild(_target(data["child"]), _optional_target(data["after"]))
    if kind == "close":
        ref = data["duplicate_of_ref"]
        duplicate = None if ref is None else _target(ref)
        if duplicate is not None and not isinstance(duplicate, Existing):
            raise InvalidChangeError("a duplicate's original is an existing issue")
        duplicate_id = data["duplicate_of_id"]
        if duplicate_id is not None and not _is_int(duplicate_id):
            raise InvalidChangeError("duplicate_of_id must be a number")
        return Close(
            _reason(data["reason"]),
            cast(int | None, duplicate_id),
            duplicate,
            _optional_text(data, "comment"),
            _flag(data, "only_if_open"),
        )
    if kind == "comment":
        return Comment(_text(data, "body"))
    if kind == "ensure_label":
        return EnsureLabel(
            _text(data, "repo"),
            _text(data, "name"),
            _text(data, "color"),
            _text(data, "description"),
            _entry(data["schema_entry"]),
        )
    if kind == "create_issue":
        labels = data["labels"]
        if not isinstance(labels, list) or not all(isinstance(n, str) for n in labels):
            raise InvalidChangeError("labels must be a list of label names")
        return CreateIssue(
            _text(data, "temp_id"),
            _text(data, "repo"),
            _text(data, "title"),
            _text(data, "body"),
            tuple(labels),
        )
    raise InvalidChangeError(f"unknown change kind {kind!r}")


def parse_change(data: Mapping[str, object]) -> Change:
    """A change from its JSON (``change_json``'s shape).

    Raises:
        InvalidChangeError: an unknown kind, a missing or mistyped field, or an invalid value.
    """
    try:
        return _parse(data)
    except (KeyError, TypeError) as exc:
        raise InvalidChangeError(f"a change is missing or mistypes {exc}") from exc


def parse_issue_change(data: Mapping[str, object]) -> IssueChange:
    """A per-issue change from its JSON (what the tray stores).

    Raises:
        InvalidChangeError: as ``parse_change``, or a repo-level change.
    """
    change = parse_change(data)
    if isinstance(change, EnsureLabel | CreateIssue):
        raise InvalidChangeError("a repo-level change can't be staged for an issue")
    return change


def batch_json(batch: ChangeBatch) -> Json:
    return {
        "issues": [
            {"target": target_json(t), "changes": [change_json(c) for c in changes]}
            for t, changes in batch.issues
        ],
        "repo": [change_json(c) for c in batch.repo],
    }


def parse_batch(data: Mapping[str, object]) -> ChangeBatch:
    """A batch from ``batch_json``'s shape.

    Raises:
        InvalidChangeError: anything malformed.
    """
    try:
        issues = cast(list[dict[str, object]], data["issues"])
        repo = [parse_change(c) for c in cast(list[dict[str, object]], data["repo"])]
        return ChangeBatch(
            tuple(
                (
                    _target(item["target"]),
                    tuple(
                        parse_issue_change(c)
                        for c in cast(list[dict[str, object]], item["changes"])
                    ),
                )
                for item in issues
            ),
            tuple(c for c in repo if isinstance(c, EnsureLabel | CreateIssue)),
        )
    except (KeyError, TypeError) as exc:
        raise InvalidChangeError(f"a batch is missing or mistypes {exc}") from exc


def parse_step_state(value: object) -> StepState:
    """Raises: InvalidChangeError: not a step state."""
    for state in STEP_STATES:
        if value == state:
            return state
    raise InvalidChangeError(f"a step state is one of {', '.join(STEP_STATES)}")


def parse_batch_status(value: object) -> BatchStatus:
    """Raises: InvalidChangeError: not a batch status."""
    for status in BATCH_STATUSES:
        if value == status:
            return status
    raise InvalidChangeError(f"a batch status is one of {', '.join(BATCH_STATUSES)}")


def result_json(result: BatchResult) -> Json:
    return {
        "results": [
            {"state": r.state, "cause": r.cause, "is_conflict": r.is_conflict}
            for r in result.results
        ],
        "created": {
            temp: {"repo": c.repo, "n": c.n, "id": c.id} for temp, c in result.created.items()
        },
        "stopped": result.stopped,
        "is_rate_limited": result.is_rate_limited,
        "rate_limited_until": None
        if result.rate_limited_until is None
        else result.rate_limited_until.isoformat(),
    }


def parse_result(batch: ChangeBatch, data: Mapping[str, object]) -> BatchResult:
    """A stored result, matched step by step with ``series(batch)``.

    Raises:
        InvalidChangeError: it doesn't match the batch's steps.
    """
    steps = series(batch)
    rows = cast(list[dict[str, object]], data["results"])
    if len(rows) != len(steps):
        raise InvalidChangeError("a stored result doesn't match its batch")
    results = tuple(
        StepResult(
            step,
            parse_step_state(row["state"]),
            cast(str | None, row["cause"]),
            bool(row.get("is_conflict", False)),
        )
        for step, row in zip(steps, rows, strict=True)
    )
    created = {
        temp: CreatedIssue(str(c["repo"]), cast(int, c["n"]), cast(int, c["id"]))
        for temp, c in cast(dict[str, dict[str, object]], data["created"]).items()
    }
    until = data["rate_limited_until"]
    return BatchResult(
        results,
        created,
        stopped=cast(str | None, data["stopped"]),
        is_rate_limited=bool(data["is_rate_limited"]),
        rate_limited_until=None if until is None else datetime.fromisoformat(str(until)),
    )
