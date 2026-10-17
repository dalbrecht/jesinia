# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suggestion drafts (spec §3.1, §3.2, §6.2; plan Task 5). Pure: no I/O.

An agent's triage of an issue is advice. It becomes **drafts** here and nothing else: a draft
changes GitHub only once the author approves it (Task 6's apply engine). What a reply may draft
is decided by Jesinia, not by the reply:

- **labels** only for the issue's *missing* dimensions (a present or conflicted one is left to
  the author), each mapped onto the repo's schema. Readiness, priority and type must be schema
  values (or their aliases); an area outside the schema is drafted as ``new_area`` (spec §6.2
  "New areas"), since approving it adds it to the schema;
- **duplicates** only among the candidates Jesinia's own similarity search found
  (``similarity.py``), never the issue itself;
- **the parent epic** only among the open epics Jesinia listed, for an issue that has no parent
  and isn't an epic;
- **readiness gaps:** for an issue that is (or is being drafted as) ``agent-ready``, what's
  missing before it really is; for a ``human-needed`` one, that it could be ``agent-ready``.

Every reason is one line. Later tasks add drafts that aren't about one issue (priority order,
groups, queue proposals, briefs): their kinds are known here so the store accepts them, and they
carry a ``scope`` instead of ``repo`` and ``n``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast, get_args

from jesinia.apps.issues.domain.schema import (
    AREA,
    PRIORITY,
    READINESS,
    TYPE,
    Canonical,
    LabelSchema,
    label_key,
)
from jesinia.apps.issues.domain.triage import Triage
from jesinia.services.github.domain.issue import InvalidLabelError, validate_label_filter
from jesinia.services.github.domain.repo import InvalidRepoError, validate_repo

SuggestionKind = Literal[
    "label",
    "duplicate",
    "parent",
    "readiness_gap",
    "new_area",
    # Later tasks (ruling IR11): priority (Task 7), group/split/brief (Task 8), queues (Task 9).
    "priority",
    "group",
    "split",
    "queue_next",
    "queue_order",
    "queue_split",
    "brief",
]
SUGGESTION_KINDS: tuple[SuggestionKind, ...] = get_args(SuggestionKind)
ISSUE_KINDS: tuple[SuggestionKind, ...] = (
    "label",
    "duplicate",
    "parent",
    "readiness_gap",
    "new_area",
)
SuggestionState = Literal["draft", "edited", "approved", "dismissed"]
SUGGESTION_STATES: tuple[SuggestionState, ...] = get_args(SuggestionState)
# What the author still has to decide on.
OPEN_STATES: tuple[SuggestionState, ...] = ("draft", "edited")
LabelDimension = Literal["readiness", "priority", "type", "area"]

AGENT_READY = "agent-ready"
HUMAN_NEEDED = "human-needed"
REASON_MAX = 300
GAP_MAX = 200
GAPS_MAX = 10
DUPLICATES_MAX = 5
SIMILARITY_DIGITS = 3


class SuggestionDomainError(ValueError):
    """Base for invalid suggestion requests."""


class InvalidSuggestionFilterError(SuggestionDomainError):
    """An unknown kind or state, or a filter or request that can't be met."""


class SuggestionNotFoundError(SuggestionDomainError):
    """No suggestion with that id."""


class SuggestionStateError(SuggestionDomainError):
    """The suggestion was already approved or dismissed: it can't change any more."""


class InvalidSuggestionEditError(SuggestionDomainError):
    """An edit the suggestion's kind doesn't allow, or a value the repo's schema lacks."""


class SuggestionRunError(RuntimeError):
    """Base for a suggestion run that produced nothing; the cause is kept."""


class SuggestionRunnerUnavailableError(SuggestionRunError):
    """No agent runner is available: suggestions wait, manual triage carries on (spec §8)."""


class InvalidAgentOutputError(SuggestionRunError):
    """The agent's reply isn't one JSON object valid against the job's schema."""


class SuggestionJobFailedError(SuggestionRunError):
    """The run failed: an invalid reply after its one retry, a timeout, or the runner's own
    error (the message says which)."""


def parse_kind(value: str) -> SuggestionKind:
    for kind in SUGGESTION_KINDS:
        if value == kind:
            return kind
    raise InvalidSuggestionFilterError(f"kind must be one of {', '.join(SUGGESTION_KINDS)}")


def parse_state(value: str) -> SuggestionState:
    for state in SUGGESTION_STATES:
        if value == state:
            return state
    raise InvalidSuggestionFilterError(f"state must be one of {', '.join(SUGGESTION_STATES)}")


def one_line(text: str, limit: int = REASON_MAX) -> str:
    """``text`` on one line (whitespace runs collapsed), at most ``limit`` characters."""
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class Suggestion:
    """A stored draft. ``repo`` and ``n`` name the issue it is about; a draft about several
    issues or a queue (later tasks) has ``scope`` instead. ``payload`` is Jesinia's own JSON,
    shaped per kind (see ``triage_drafts``)."""

    id: str
    repo: str | None
    n: int | None
    scope: str | None
    kind: SuggestionKind
    payload: Mapping[str, object]
    reason: str
    state: SuggestionState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Draft:
    """What a run proposes for one issue, before it is stored."""

    kind: SuggestionKind
    payload: dict[str, object]
    reason: str


@dataclass(frozen=True)
class SuggestionFilters:
    """Which suggestions to list: one issue (``repo`` and ``n``), one repo, or all; ``states``
    (all of them when empty); one ``kind``."""

    repo: str | None = None
    n: int | None = None
    states: tuple[SuggestionState, ...] = OPEN_STATES
    kind: SuggestionKind | None = None

    def __post_init__(self) -> None:
        if self.repo is not None:
            validate_repo(self.repo)
        if self.n is not None and self.repo is None:
            raise InvalidSuggestionFilterError("an issue number needs its repo")


# The agent's reply ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelProposal:
    dimension: str
    value: str
    reason: str


@dataclass(frozen=True)
class RefProposal:
    repo: str
    number: int
    reason: str


@dataclass(frozen=True)
class ReadinessProposal:
    ready: bool
    missing: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class TriageReply:
    """A triage reply already valid against ``schemas/triage.json``."""

    labels: tuple[LabelProposal, ...]
    duplicates: tuple[RefProposal, ...]
    parent: RefProposal | None
    agent_ready: ReadinessProposal

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> TriageReply:
        labels = cast(list[dict[str, str]], data["labels"])
        duplicates = cast(list[dict[str, object]], data["duplicates"])
        parent = cast(dict[str, object] | None, data["parent"])
        ready = cast(dict[str, object], data["agent_ready"])
        return cls(
            labels=tuple(LabelProposal(i["dimension"], i["value"], i["reason"]) for i in labels),
            duplicates=tuple(_ref(item) for item in duplicates),
            parent=None if parent is None else _ref(parent),
            agent_ready=ReadinessProposal(
                bool(ready["ready"]),
                tuple(cast(list[str], ready["missing"])),
                str(ready["reason"]),
            ),
        )


def _ref(item: Mapping[str, object]) -> RefProposal:
    return RefProposal(str(item["repo"]), cast(int, item["number"]), str(item["reason"]))


@dataclass(frozen=True)
class Candidate:
    """A possible duplicate Jesinia's similarity search found; ``repo`` as GitHub spells it."""

    repo: str
    n: int
    score: float


@dataclass(frozen=True)
class TriageContext:
    """What Jesinia knows about the issue being triaged. ``candidates`` and ``epics`` are keyed
    by ``(repo.casefold(), number)``; an epic's value is ``(repo, number)`` as GitHub spells
    it."""

    repo: str
    n: int
    triage: Triage
    schema: LabelSchema
    candidates: Mapping[tuple[str, int], Candidate]
    epics: Mapping[tuple[str, int], tuple[str, int]]
    has_parent: bool

    @property
    def key(self) -> tuple[str, int]:
        return (self.repo.casefold(), self.n)


def _values(schema: LabelSchema, dimension: str) -> tuple[Canonical, ...]:
    by_dimension = {
        READINESS: schema.readiness,
        PRIORITY: schema.priority,
        TYPE: schema.type,
        AREA: schema.areas,
    }
    return by_dimension[dimension]


def _canonical(schema: LabelSchema, dimension: str, value: str) -> str | None:
    """The schema's canonical name for ``value`` in ``dimension`` (aliases count)."""
    wanted = label_key(value)
    for canonical in _values(schema, dimension):
        if any(label_key(name) == wanted for name in (canonical.name, *canonical.aliases)):
            return canonical.name
    return None


def _area(schema: LabelSchema, value: str) -> tuple[SuggestionKind, str] | None:
    """An area value as a draft: a schema area (``label``), else a new one (``new_area``),
    prefixed when written without the prefix; ``None`` for a name GitHub can't hold or list."""
    known = _canonical(schema, AREA, value)
    if known is not None:
        return "label", known
    name = value.strip()
    if not label_key(name).startswith(label_key(schema.area_prefix)):
        name = f"{schema.area_prefix}{name}"
    if len(label_key(name)) <= len(label_key(schema.area_prefix)):
        return None
    try:
        validate_label_filter(name)
    except InvalidLabelError:
        return None
    known = _canonical(schema, AREA, name)
    return ("new_area", name) if known is None else ("label", known)


def _label_draft(
    schema: LabelSchema, dimension: str, value: str
) -> tuple[SuggestionKind, dict[str, object]] | None:
    if dimension == AREA:
        area = _area(schema, value)
        if area is None:
            return None
        kind, name = area
        return kind, {"dimension": AREA, "value": name}
    canonical = _canonical(schema, dimension, value)
    return None if canonical is None else ("label", {"dimension": dimension, "value": canonical})


def triage_drafts(reply: TriageReply, ctx: TriageContext) -> list[Draft]:
    """The drafts ``reply`` may produce for the issue in ``ctx`` (see the module docstring);
    everything else in the reply is dropped. Order: labels, duplicates, parent, readiness."""
    drafts = _labels(reply.labels, ctx)
    drafts += _duplicates(reply.duplicates, ctx)
    drafts += _parent(reply.parent, ctx)
    drafted_readiness = next(
        (str(d.payload["value"]) for d in drafts if d.payload.get("dimension") == READINESS),
        None,
    )
    drafts += _readiness(reply.agent_ready, ctx, ctx.triage.readiness or drafted_readiness)
    return drafts


def _labels(proposals: Sequence[LabelProposal], ctx: TriageContext) -> list[Draft]:
    drafts: list[Draft] = []
    seen: set[tuple[object, ...]] = set()
    for proposal in proposals:
        reason = one_line(proposal.reason)
        if proposal.dimension not in ctx.triage.missing or not reason:
            continue
        made = _label_draft(ctx.schema, proposal.dimension, proposal.value)
        if made is None:
            continue
        kind, payload = made
        key = identity(kind, payload)
        if key in seen:
            continue
        seen.add(key)
        drafts.append(Draft(kind, payload, reason))
    return drafts


def _duplicates(proposals: Sequence[RefProposal], ctx: TriageContext) -> list[Draft]:
    drafts: list[Draft] = []
    seen: set[tuple[str, int]] = set()
    for proposal in proposals:
        key = (proposal.repo.casefold(), proposal.number)
        candidate = ctx.candidates.get(key)
        reason = one_line(proposal.reason)
        if candidate is None or key == ctx.key or key in seen or not reason:
            continue
        seen.add(key)
        score = round(candidate.score, SIMILARITY_DIGITS)
        payload: dict[str, object] = {"repo": candidate.repo, "n": candidate.n, "similarity": score}
        drafts.append(Draft("duplicate", payload, reason))
    return drafts[:DUPLICATES_MAX]


def _parent(proposal: RefProposal | None, ctx: TriageContext) -> list[Draft]:
    if proposal is None or ctx.has_parent or ctx.triage.is_epic:
        return []
    key = (proposal.repo.casefold(), proposal.number)
    epic = ctx.epics.get(key)
    reason = one_line(proposal.reason)
    if epic is None or key == ctx.key or not reason:
        return []
    repo, n = epic
    return [Draft("parent", {"repo": repo, "n": n}, reason)]


def _readiness(
    proposal: ReadinessProposal, ctx: TriageContext, readiness: str | None
) -> list[Draft]:
    reason = one_line(proposal.reason)
    if ctx.triage.is_epic or not reason:
        return []
    if readiness == AGENT_READY and not proposal.ready:
        missing = [gap for gap in (one_line(m, GAP_MAX) for m in proposal.missing) if gap]
        if missing:
            payload: dict[str, object] = {"ready": False, "missing": missing[:GAPS_MAX]}
            return [Draft("readiness_gap", payload, reason)]
    if readiness == HUMAN_NEEDED and proposal.ready:
        return [Draft("readiness_gap", {"ready": True, "missing": []}, reason)]
    return []


# Reruns, edits and dismissals ----------------------------------------------------------------


def identity(kind: SuggestionKind, payload: Mapping[str, object]) -> tuple[object, ...]:
    """What makes two suggestions "the same advice" for one issue: one per single-value
    dimension (any value), one per area, one per duplicate or parent, one readiness gap."""
    if kind in ("label", "new_area"):
        dimension = str(payload["dimension"])
        if dimension == AREA:
            return ("label", AREA, label_key(str(payload["value"])))
        return ("label", dimension)
    if kind in ("duplicate", "parent"):
        return (kind, str(payload["repo"]).casefold(), payload["n"])
    return (kind,)


def fresh_drafts(drafts: Sequence[Draft], kept: Sequence[Suggestion]) -> list[Draft]:
    """``drafts`` without the advice the author already handled (edited, approved or
    dismissed): a rerun never brings back what was dismissed nor doubles what was edited."""
    handled = {identity(s.kind, s.payload) for s in kept}
    return [d for d in drafts if identity(d.kind, d.payload) not in handled]


def _require_open(suggestion: Suggestion) -> None:
    if suggestion.state not in OPEN_STATES:
        raise SuggestionStateError(f"suggestion {suggestion.id} is {suggestion.state} already")


def edit(
    suggestion: Suggestion,
    payload: Mapping[str, object],
    reason: str | None,
    schema: LabelSchema,
    now: datetime,
) -> Suggestion:
    """``suggestion`` with the author's ``payload`` (and ``reason``, when given), validated for
    its kind against the repo's ``schema``. An area edit can turn a ``label`` into a
    ``new_area`` and back.

    Raises:
        SuggestionStateError: it was approved or dismissed already.
        InvalidSuggestionEditError: the payload doesn't fit the kind or the schema.
    """
    _require_open(suggestion)
    kind, checked = _edited(suggestion, payload, schema)
    new_reason = suggestion.reason
    if reason is not None:
        new_reason = one_line(reason)
        if not new_reason:
            raise InvalidSuggestionEditError("a reason can't be blank")
    return replace(
        suggestion,
        kind=kind,
        payload=checked,
        reason=new_reason,
        state="edited",
        updated_at=now,
    )


def _keys(payload: Mapping[str, object], *names: str) -> None:
    if set(payload) != set(names):
        raise InvalidSuggestionEditError(f"the payload must have exactly {', '.join(names)}")


def _edited(
    suggestion: Suggestion, payload: Mapping[str, object], schema: LabelSchema
) -> tuple[SuggestionKind, dict[str, object]]:
    kind = suggestion.kind
    if kind in ("label", "new_area"):
        return _edited_label(suggestion, payload, schema)
    if kind in ("duplicate", "parent"):
        _keys(payload, "repo", "n")
        repo, n = payload["repo"], payload["n"]
        if not isinstance(repo, str) or not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise InvalidSuggestionEditError("repo must be owner/name and n a positive number")
        try:
            validate_repo(repo)
        except InvalidRepoError as exc:
            raise InvalidSuggestionEditError(str(exc)) from exc
        return kind, {"repo": repo, "n": n}
    if kind == "readiness_gap":
        return kind, _edited_gap(payload)
    raise InvalidSuggestionEditError(f"a {kind} suggestion can't be edited yet")


def _edited_label(
    suggestion: Suggestion, payload: Mapping[str, object], schema: LabelSchema
) -> tuple[SuggestionKind, dict[str, object]]:
    _keys(payload, "dimension", "value")
    dimension, value = payload["dimension"], payload["value"]
    if dimension != suggestion.payload["dimension"] or not isinstance(value, str):
        raise InvalidSuggestionEditError("an edit keeps the dimension and gives a label value")
    made = _label_draft(schema, str(dimension), value)
    if made is None:
        raise InvalidSuggestionEditError(f"{value!r} isn't a {dimension} value of this repo")
    return made


def _edited_gap(payload: Mapping[str, object]) -> dict[str, object]:
    _keys(payload, "ready", "missing")
    ready, missing = payload["ready"], payload["missing"]
    if not isinstance(ready, bool) or not isinstance(missing, list):
        raise InvalidSuggestionEditError("ready must be true or false and missing a list")
    if len(missing) > GAPS_MAX or not all(isinstance(item, str) for item in missing):
        raise InvalidSuggestionEditError(f"missing holds at most {GAPS_MAX} texts")
    gaps = [one_line(str(item), GAP_MAX) for item in missing]
    if not all(gaps):
        raise InvalidSuggestionEditError("a missing item can't be blank")
    return {"ready": ready, "missing": gaps}


def dismiss(suggestion: Suggestion, now: datetime) -> Suggestion:
    """``suggestion`` dismissed; dismissing it again changes nothing.

    Raises:
        SuggestionStateError: it was approved already.
    """
    if suggestion.state == "dismissed":
        return suggestion
    _require_open(suggestion)
    return replace(suggestion, state="dismissed", updated_at=now)


def payload_json(payload: Mapping[str, object]) -> str:
    """How a payload is stored (keys sorted, so equal payloads store equally)."""
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)
