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
  that doesn't exist yet, then for each alias a ``DeleteLabel`` with that canonical as its
  ``replacement``. Membership never comes from the cache (ruling IR31): the preview lists the
  alias's issues and pull requests live, for the author to see, and the executor lists them live
  again right before the delete, moves every one to the canonical, and deletes only when every
  move (for every alias) succeeded. A label added on GitHub after the last sync, or carried by
  a pull request (never cached), is moved, not stripped.
- **Plans:** a preview stores its ops under an id for ``PLAN_TTL``. An apply must name a plan
  that exists, hasn't expired and is of the same kind, and the ops planned again from the repo's
  current state must have the same shape (labels created, deleted and their replacements; the
  member lists are re-read anyway): a label created or deleted on GitHub, or a schema change,
  since the preview asks for a fresh preview.
- **Names** compare by ``label_key`` (NFC, casefolded). Two GitHub labels with one key are
  refused rather than guessed between.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
    label_key,
    write_name,
)

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


class LabelNameCollisionError(LabelPlanError):
    """Two labels on GitHub compare as one name (case or Unicode form); Jesinia won't guess
    which one a step means."""


@dataclass(frozen=True)
class CreateLabel:
    """Create a label on GitHub (a no-op when it exists by the time it runs)."""

    name: str
    color: str
    description: str


@dataclass(frozen=True)
class DeleteLabel:
    """Delete a label from the repo (and so from every issue). With a ``replacement`` the
    executor first lists the label's issues and pull requests live and moves each to the
    replacement, and deletes only when every move succeeded. ``members`` is what the preview
    listed (shown to the author; never trusted at apply time)."""

    name: str
    replacement: str | None = None
    members: tuple[int, ...] = ()


LabelOp = CreateLabel | DeleteLabel


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


def _on_github(existing: Iterable[str]) -> dict[str, str]:
    """GitHub's labels by ``label_key``.

    Raises:
        LabelNameCollisionError: two labels share a key.
    """
    found: dict[str, str] = {}
    for name in existing:
        key = label_key(name)
        if key in found:
            raise LabelNameCollisionError(
                f"{found[key]!r} and {name!r} on GitHub compare as the same label; rename one "
                "on GitHub first"
            )
        found[key] = name
    return found


def plan_setup(schema: LabelSchema, existing: Iterable[str]) -> list[CreateLabel]:
    """The canonical labels to create so every schema value can be written on the repo whose
    labels are ``existing``.

    Raises:
        LabelNameCollisionError: two of ``existing`` compare as one name.
    """
    names = list(_on_github(existing).values())
    return [
        _create(dimension, canonical)
        for dimension, canonical in schema_labels(schema)
        if write_name(canonical, names) is None
    ]


def migration_aliases(schema: LabelSchema, existing: Iterable[str]) -> list[tuple[str, str]]:
    """``(alias as spelled on GitHub, canonical)`` for every alias on GitHub, in schema order.

    Raises:
        LabelNameCollisionError: two of ``existing`` compare as one name.
    """
    on_github = _on_github(existing)
    return [
        (on_github[label_key(alias)], canonical.name)
        for _, canonical in schema_labels(schema)
        for alias in canonical.aliases
        if label_key(alias) in on_github
    ]


@dataclass(frozen=True)
class MigrationPlan:
    """What migrating a repo's aliases to their canonical labels does, in order."""

    create: tuple[CreateLabel, ...]
    delete: tuple[DeleteLabel, ...]

    @property
    def relabel(self) -> dict[int, tuple[str, ...]]:
        """Each listed issue or pull request and the aliases it will lose (for the preview)."""
        found: dict[int, tuple[str, ...]] = {}
        for op in self.delete:
            for number in op.members:
                found[number] = (*found.get(number, ()), op.name)
        return found

    def ops(self) -> list[LabelOp]:
        return [*self.create, *self.delete]


def plan_migration(
    schema: LabelSchema, existing: Iterable[str], members: Mapping[str, Sequence[int]]
) -> MigrationPlan:
    """Plan a migration: ``existing`` is the repo's labels on GitHub and ``members`` the issue
    and pull request numbers listed live for each alias (keyed as GitHub spells it).

    Raises:
        LabelNameCollisionError: two of ``existing`` compare as one name.
    """
    names = list(existing)
    on_github = _on_github(names)
    create: list[CreateLabel] = []
    delete: list[DeleteLabel] = []
    for dimension, canonical in schema_labels(schema):
        present = [on_github[label_key(a)] for a in canonical.aliases if label_key(a) in on_github]
        if not present:
            continue
        if label_key(canonical.name) not in on_github:
            create.append(_create(dimension, canonical))
        delete.extend(
            DeleteLabel(alias, canonical.name, tuple(members.get(alias, ()))) for alias in present
        )
    return MigrationPlan(tuple(create), tuple(delete))


def _shape(op: LabelOp) -> LabelOp:
    return replace(op, members=()) if isinstance(op, DeleteLabel) else op


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
        if tuple(map(_shape, current)) != tuple(map(_shape, self.ops)):
            raise LabelPlanOutdatedError(
                f"{self.repo} changed since plan {self.id} was previewed (its labels or "
                "schema); preview again and approve what would run now"
            )
