# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label setup and migration plans (spec §2, §6.2).

Pure: no I/O. Jesinia never creates a label implicitly: canonical labels are created only in an
approved **setup** batch, and aliases are renamed only in an approved **migration** batch. Both
are a list of ``LabelOp`` that a preview shows and an apply runs in order (the executor in
``application/labels.py``, which Task 6's apply engine reuses for its ``EnsureLabel`` steps).

- **Setup** creates each canonical label Jesinia couldn't write today: neither its name nor any
  of its aliases exists on GitHub (``write_name`` would give ``None``). A canonical reachable
  through an existing alias is not missing; migrating renames it.
- **Migration** maps every alias that exists on GitHub to its canonical: it creates a canonical
  that doesn't exist yet, relabels every cached issue carrying an alias (add the canonical,
  remove the alias) and then deletes the alias labels. The executor deletes only after every
  earlier step succeeded, so no issue can lose a label it wasn't given back.
- **Plans:** a preview stores its ops under an id for ``PLAN_TTL``. An apply must name a plan
  that exists, hasn't expired and is of the same kind, and the ops planned again from the repo's
  current state must equal the stored ones: anything changed since the preview (a label created
  or deleted on GitHub, the schema, the cached issues) asks for a fresh preview.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, get_args

from jesinia.apps.issues.domain.schema import (
    AREA,
    HIERARCHY,
    PRIORITY,
    READINESS,
    TYPE,
    Canonical,
    LabelSchema,
    write_name,
)
from jesinia.services.github.domain.issue import Issue, IssueRef

LabelPlanKind = Literal["setup", "migrate"]
PLAN_KINDS: tuple[LabelPlanKind, ...] = get_args(LabelPlanKind)
PLAN_TTL = timedelta(minutes=15)

# GitHub label colours per dimension, so a created set reads as one convention.
_COLORS: Mapping[str, str] = {
    READINESS: "0e8a16",
    PRIORITY: "d93f0b",
    TYPE: "1d76db",
    AREA: "c5def5",
    HIERARCHY: "5319e7",
}
_DIMENSION_NAMES: Mapping[str, str] = {
    READINESS: "Readiness",
    PRIORITY: "Priority",
    TYPE: "Type",
    AREA: "Area",
    HIERARCHY: "Hierarchy",
}


class LabelPlanError(ValueError):
    """Base for label setup and migration refusals."""


class InvalidLabelPlanKindError(LabelPlanError):
    """Not ``setup`` or ``migrate``."""


class LabelPlanNotFoundError(LabelPlanError):
    """No previewed plan with that id for this repo (never previewed, or already applied)."""


class LabelPlanExpiredError(LabelPlanError):
    """The preview is older than ``PLAN_TTL``."""


class LabelPlanKindMismatchError(LabelPlanError):
    """The plan was previewed as the other kind."""


class LabelPlanOutdatedError(LabelPlanError):
    """The repo changed since the preview; what was approved is no longer what would run."""


class RepoNotSyncedError(LabelPlanError):
    """A migration relabels issues from the cache, so the repo must have finished a sync."""


@dataclass(frozen=True)
class CreateLabel:
    """Create a label on GitHub (a no-op when it exists by the time it runs)."""

    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Relabel:
    """On issue ``number``, replace each ``(old, new)`` label present on a fresh read: add the
    new ones in one write, then remove the old ones."""

    number: int
    replace: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DeleteLabel:
    """Delete a label from the repo (and so from every issue); runs only when every earlier
    step of the batch succeeded."""

    name: str


LabelOp = CreateLabel | Relabel | DeleteLabel


def parse_plan_kind(value: str) -> LabelPlanKind:
    for kind in PLAN_KINDS:
        if value == kind:
            return kind
    raise InvalidLabelPlanKindError(f"kind must be one of {', '.join(PLAN_KINDS)}, got {value!r}")


def schema_labels(schema: LabelSchema) -> list[tuple[str, Canonical]]:
    """Every canonical label of ``schema`` with its dimension, in schema order."""
    dimensions: tuple[tuple[str, Iterable[Canonical]], ...] = (
        (READINESS, schema.readiness),
        (PRIORITY, schema.priority),
        (TYPE, schema.type),
        (AREA, schema.areas),
        (HIERARCHY, (schema.epic, schema.initiative)),
    )
    return [(dimension, value) for dimension, values in dimensions for value in values]


def _create(dimension: str, canonical: Canonical) -> CreateLabel:
    return CreateLabel(
        canonical.name,
        _COLORS[dimension],
        f"{_DIMENSION_NAMES[dimension]} (Jesinia label schema)",
    )


def _key(label: str) -> str:
    return label.strip().casefold()


def plan_setup(schema: LabelSchema, existing: Iterable[str]) -> list[CreateLabel]:
    """The canonical labels to create so every schema value can be written on the repo whose
    labels are ``existing``."""
    names = list(existing)
    return [
        _create(dimension, canonical)
        for dimension, canonical in schema_labels(schema)
        if write_name(canonical, names) is None
    ]


@dataclass(frozen=True)
class MigrationPlan:
    """What migrating a repo's aliases to their canonical labels does, in order."""

    create: tuple[CreateLabel, ...]
    relabel: Mapping[IssueRef, Relabel]
    delete: tuple[str, ...]

    def ops(self) -> list[LabelOp]:
        return [*self.create, *self.relabel.values(), *(DeleteLabel(n) for n in self.delete)]


def plan_migration(
    repo: str, schema: LabelSchema, existing: Iterable[str], issues: Iterable[Issue]
) -> MigrationPlan:
    """Plan the migration of ``repo``: ``existing`` is its labels on GitHub, ``issues`` every
    cached issue (open and closed). Old labels keep GitHub's own spelling."""
    on_github = {_key(name): name for name in existing}
    to_canonical: dict[str, str] = {}  # casefolded alias on GitHub -> canonical
    create: list[CreateLabel] = []
    delete: list[str] = []
    for dimension, canonical in schema_labels(schema):
        present = [_key(alias) for alias in canonical.aliases if _key(alias) in on_github]
        if not present:
            continue
        if _key(canonical.name) not in on_github:
            create.append(_create(dimension, canonical))
        for alias in present:
            to_canonical[alias] = canonical.name
            delete.append(on_github[alias])
    relabel: dict[IssueRef, Relabel] = {}
    for item in issues:
        pairs = tuple(
            (label, to_canonical[_key(label)])
            for label in item.labels
            if _key(label) in to_canonical
        )
        if pairs:
            relabel[IssueRef(repo, item.number, item.id)] = Relabel(item.number, pairs)
    return MigrationPlan(tuple(create), relabel, tuple(delete))


@dataclass(frozen=True)
class LabelPlan:
    """A previewed batch, kept under ``id`` until applied or expired."""

    id: str
    repo: str
    kind: LabelPlanKind
    ops: tuple[LabelOp, ...]
    created_at: datetime

    @property
    def expires_at(self) -> datetime:
        return self.created_at + PLAN_TTL

    def check(self, kind: LabelPlanKind, now: datetime, current: tuple[LabelOp, ...]) -> None:
        """Refuse an apply as ``kind`` at ``now`` when the repo's state now plans ``current``.

        Raises:
            LabelPlanKindMismatchError: previewed as the other kind.
            LabelPlanExpiredError: older than ``PLAN_TTL``.
            LabelPlanOutdatedError: ``current`` differs from what was previewed.
        """
        if kind != self.kind:
            raise LabelPlanKindMismatchError(
                f"plan {self.id} was previewed as {self.kind}, not {kind}"
            )
        if now > self.expires_at:
            raise LabelPlanExpiredError(
                f"plan {self.id} expired at {self.expires_at.isoformat()}; preview again"
            )
        if current != self.ops:
            raise LabelPlanOutdatedError(
                f"{self.repo} changed since plan {self.id} was previewed (labels, schema or "
                "issues); preview again and approve what would run now"
            )
