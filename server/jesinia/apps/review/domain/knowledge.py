# SPDX-License-Identifier: AGPL-3.0-or-later
"""The author's knowledge (spec §6.1, plan Task 14): units, signals and the familiarity maths.

Pure: exposures, the clock and counts in, a score out.

- **Units:** a repo directory up to ``MAX_UNIT_DEPTH`` folders deep (``(repo, path_prefix)``,
  the repo root included), or a KB tag topic.
- **Signals:** ``Exposure(kind, at)``; a file a signal touches exposes every prefix of its
  folder, so knowing ``src/sync/a`` also counts towards ``src/sync``, ``src`` and the repo.
- **Familiarity** (the plan's formula, spec §12) in [0, 1]:

  - ``recency``: each exposure weighs ``KIND_WEIGHTS[kind]``, halved every ``HALF_LIFE``;
    exposures combine as independent evidence, ``1 - Π(1 - weight · decay)``, so more of
    them means more familiarity without ever passing 1;
  - times ``1 - c / (c + CHANGE_SCALE)``, ``c`` the lines changed in the unit since the
    author's last exposure;
  - times ``1 - distance``, the share of the unit's path not shared with the author's
    ``TOP_UNITS`` best-known units;
  - an override wins: ``known`` is 1.0, ``unknown`` 0.0.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from jesinia.apps.review.domain.errors import ReviewError
from jesinia.apps.review.domain.workspace import WorkspaceError, canonical_repo
from jesinia.services.kb.domain.tags import is_valid_tag

ExposureKind = Literal["commit", "review_quick", "review_deep", "quiz_pass", "quiz_fail", "kb_page"]
EXPOSURE_KINDS: tuple[ExposureKind, ...] = (
    "commit",
    "review_quick",
    "review_deep",
    "quiz_pass",
    "quiz_fail",
    "kb_page",
)
Override = Literal["known", "unknown"]
OVERRIDES: tuple[Override, ...] = ("known", "unknown")
UnitKind = Literal["path", "tag"]

HALF_LIFE = timedelta(days=90)
# The plan lists no weight for a failed quiz: it is recorded (and shown), but adds nothing.
KIND_WEIGHTS: Mapping[ExposureKind, float] = {
    "review_deep": 1.0,
    "review_quick": 0.4,
    "commit": 0.8,
    "quiz_pass": 0.6,
    "kb_page": 0.5,
    "quiz_fail": 0.0,
}
CHANGE_SCALE = 200
MAX_UNIT_DEPTH = 3
TOP_UNITS = 10
# A quiz passes with at least this share of the answered questions right (builder's call).
QUIZ_PASS_SHARE = 0.6
QUIZ_MAX_QUESTIONS = 10
# History older than this adds less than 1/256 of a weight: it is neither read nor kept.
HISTORY_HORIZON = timedelta(days=730)
# Entries of one KB page's ``jesinia.code`` read (the page is untrusted).
CODE_ENTRIES_MAX = 50
COMPONENT_MAX = 100
# The summary is trusted text in every agent's ContextPack: bounded in size.
SUMMARY_CHARS = 2_000
SUMMARY_UNITS = 20
SUMMARY_TOPICS = 5

_UNIT_REPO = "repo/"
_UNIT_TAG = "tag/"
_ROOT_LABEL = "(repo root)"
_KIND_WORDS: Mapping[ExposureKind, str] = {
    "commit": "a commit",
    "review_quick": "a quick review",
    "review_deep": "a deep review",
    "quiz_pass": "a passed quiz",
    "quiz_fail": "a failed quiz",
    "kb_page": "a KB page",
}


class KnowledgeError(ReviewError):
    """The knowledge model's base error."""


class InvalidUnitError(KnowledgeError, ValueError):
    """Text that doesn't name a unit (``repo/<owner>/<name>[/<path>]`` or ``tag/<tag>``)."""


class KnowledgeInputError(KnowledgeError, ValueError):
    """A familiarity input that cannot describe real history."""


def _component_ok(part: str) -> bool:
    return (
        bool(part)
        and part not in {".", ".."}
        and len(part) <= COMPONENT_MAX
        and "/" not in part
        and not any(unicodedata.category(ch) == "Cc" for ch in part)
    )


def _canonical(repo: str) -> str:
    try:
        return canonical_repo(repo)
    except WorkspaceError as exc:
        raise InvalidUnitError(f"not a GitHub owner/name: {repo!r}") from exc


@dataclass(frozen=True)
class Unit:
    """A unit of knowledge: ``repo`` and ``path`` (``""`` is the repo root) for a directory,
    or ``tag`` for a KB topic. Built through ``path_unit`` and ``tag_unit``, which validate."""

    repo: str | None
    path: str
    tag: str | None

    @classmethod
    def path_unit(cls, repo: str, path: str) -> Unit:
        """Raises: InvalidUnitError: a bad repo, or a path deeper than ``MAX_UNIT_DEPTH``
        or with an empty, dot or control-character component."""
        parts = path.split("/") if path else []
        if len(parts) > MAX_UNIT_DEPTH or not all(map(_component_ok, parts)):
            raise InvalidUnitError(f"not a unit path (at most {MAX_UNIT_DEPTH} folders): {path!r}")
        return cls(_canonical(repo), path, None)

    @classmethod
    def tag_unit(cls, tag: str) -> Unit:
        """Raises: InvalidUnitError: not a normalised KB tag."""
        if not is_valid_tag(tag):
            raise InvalidUnitError(f"not a KB tag: {tag!r}")
        return cls(None, "", tag)

    @property
    def kind(self) -> UnitKind:
        return "tag" if self.tag is not None else "path"

    @property
    def id(self) -> str:
        """The unit's one spelling, as the API names it."""
        if self.tag is not None:
            return f"{_UNIT_TAG}{self.tag}"
        suffix = f"/{self.path}" if self.path else ""
        return f"{_UNIT_REPO}{self.repo}{suffix}"

    @property
    def components(self) -> tuple[str, ...]:
        """The unit's path for distance: the repo, then its folders (``#tag`` for a tag)."""
        if self.tag is not None:
            return (f"#{self.tag}",)
        return (str(self.repo), *(self.path.split("/") if self.path else ()))

    @property
    def label(self) -> str:
        if self.tag is not None:
            return f"#{self.tag}"
        return self.path or _ROOT_LABEL


def parse_unit(text: str) -> Unit:
    """The unit ``text`` names (``Unit.id``'s spelling; owner and name in any case).

    Raises:
        InvalidUnitError: anything else.
    """
    if text.startswith(_UNIT_TAG):
        return Unit.tag_unit(text.removeprefix(_UNIT_TAG))
    if not text.startswith(_UNIT_REPO):
        raise InvalidUnitError(f"a unit is repo/<owner>/<name>[/<path>] or tag/<tag>: {text!r}")
    owner, _, rest = text.removeprefix(_UNIT_REPO).partition("/")
    name, slash, path = rest.partition("/")
    if slash and not path:
        raise InvalidUnitError(f"a unit path can't end with '/': {text!r}")
    return Unit.path_unit(f"{owner}/{name}", path)


def _folder_prefixes(parts: Sequence[str]) -> list[str]:
    """``""`` then each folder prefix up to ``MAX_UNIT_DEPTH``, stopping at a bad component."""
    prefixes = [""]
    for depth, part in enumerate(parts[:MAX_UNIT_DEPTH], start=1):
        if not _component_ok(part):
            break
        prefixes.append("/".join(parts[:depth]))
    return prefixes


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part and part != "."]


def folder_prefixes_of_file(file_path: str) -> list[str]:
    """The unit paths a changed file exposes: the root and its folder's prefixes."""
    return _folder_prefixes(_parts(file_path)[:-1])


def units_of_file(repo: str, file_path: str) -> tuple[Unit, ...]:
    """Every unit a file in ``repo`` exposes, from the root down to its deepest unit.

    Raises:
        InvalidUnitError: ``repo`` isn't an owner/name.
    """
    canonical = _canonical(repo)
    return tuple(Unit(canonical, p, None) for p in folder_prefixes_of_file(file_path))


def unit_of_file(repo: str, file_path: str) -> Unit:
    """The deepest unit of a file: the one its familiarity is read from."""
    return units_of_file(repo, file_path)[-1]


def code_units(jesinia: Mapping[str, object]) -> tuple[Unit, ...]:
    """The units a KB page's ``jesinia.code`` declares (spec §6.1), each with its parents.

    The page is untrusted data: only ``{repo, path?}`` mappings with a valid repo and a
    valid path are read (at most ``CODE_ENTRIES_MAX``), and nothing in them is run.
    """
    entries = jesinia.get("code")
    if not isinstance(entries, list):
        return ()
    found: dict[str, Unit] = {}
    for entry in entries[:CODE_ENTRIES_MAX]:
        if not isinstance(entry, Mapping):
            continue
        repo, path = entry.get("repo"), entry.get("path", "")
        if not isinstance(repo, str) or not isinstance(path, str):
            continue
        parts = [part for part in path.strip("/").split("/") if part]
        if not all(map(_component_ok, parts)):
            continue
        try:
            canonical = _canonical(repo)
        except InvalidUnitError:
            continue
        for prefix in _folder_prefixes(parts):
            unit = Unit(canonical, prefix, None)
            found.setdefault(unit.id, unit)
    return tuple(found.values())


@dataclass(frozen=True)
class Exposure:
    """One signal about one unit: what it was and when."""

    kind: ExposureKind
    at: datetime


@dataclass(frozen=True)
class UnitRecord:
    """A unit with every exposure recorded for it and the author's override, if any."""

    unit: Unit
    exposures: tuple[Exposure, ...]
    override: Override | None = None


@dataclass(frozen=True)
class LoggedCommit:
    """A default-branch commit as the workspace logs it: its author's email and each file
    with its changed lines (added plus deleted; 0 for a binary file)."""

    sha: str
    at: datetime
    email: str
    files: tuple[tuple[str, int], ...]


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise KnowledgeInputError("now must be timezone-aware")


def recency(exposures: Iterable[Exposure], now: datetime) -> float:
    """How recently and how much the author engaged: ``1 - Π(1 - weight · decay)``, the
    decay halving every ``HALF_LIFE`` (an exposure in the future counts as now)."""
    _aware(now)
    unseen = 1.0
    for exposure in exposures:
        age = max((now - exposure.at) / HALF_LIFE, 0.0)
        unseen *= 1.0 - KIND_WEIGHTS[exposure.kind] * 0.5**age
    return 1.0 - unseen


def last_exposure(exposures: Iterable[Exposure]) -> datetime | None:
    """The newest exposure that carries weight: changes are counted from it."""
    weighted = [e.at for e in exposures if KIND_WEIGHTS[e.kind] > 0]
    return max(weighted, default=None)


def top_units(scores: Mapping[Unit, float], limit: int = TOP_UNITS) -> tuple[Unit, ...]:
    """The ``limit`` units the author knows best (score above 0), best first, ties by id."""
    ranked = sorted((u for u, s in scores.items() if s > 0), key=lambda u: (-scores[u], u.id))
    return tuple(ranked[:limit])


def distance(unit: Unit, top: Iterable[Unit]) -> float:
    """The share of ``unit``'s path not shared with the closest of the ``top`` units."""
    mine = unit.components
    shared = 0
    for other in top:
        common = 0
        for a, b in zip(mine, other.components, strict=False):
            if a != b:
                break
            common += 1
        shared = max(shared, common)
    return 1.0 - shared / len(mine)


def familiarity(
    record: UnitRecord, now: datetime, changes_since_last: int, distance: float
) -> float:
    """The author's familiarity with ``record.unit`` in [0, 1] (see the module docstring).

    Raises:
        KnowledgeInputError: negative changes, a distance outside 0 to 1, a naive ``now``.
    """
    if changes_since_last < 0:
        raise KnowledgeInputError(f"changes can't be negative, got {changes_since_last}")
    if not 0.0 <= distance <= 1.0:  # also refuses NaN
        raise KnowledgeInputError(f"distance must be between 0 and 1, got {distance}")
    _aware(now)
    if record.override is not None:
        return 1.0 if record.override == "known" else 0.0
    changed = changes_since_last / (changes_since_last + CHANGE_SCALE)
    return recency(record.exposures, now) * (1.0 - changed) * (1.0 - distance)


def churn_by_prefix(files: Iterable[tuple[str, int]]) -> dict[str, int]:
    """Changed lines per unit path (the root included) over a commit's files."""
    churn: dict[str, int] = {}
    for path, lines in files:
        for prefix in folder_prefixes_of_file(path):
            churn[prefix] = churn.get(prefix, 0) + lines
    return churn


def quiz_kind(right: int, answered: int) -> ExposureKind:
    """``quiz_pass`` with at least ``QUIZ_PASS_SHARE`` of the answered questions right.

    Raises:
        KnowledgeInputError: nothing answered, or counts no quiz can produce.
    """
    if not 0 < answered <= QUIZ_MAX_QUESTIONS or not 0 <= right <= answered:
        raise KnowledgeInputError(f"not a quiz result: {right} right of {answered} answered")
    return "quiz_pass" if right >= QUIZ_PASS_SHARE * answered else "quiz_fail"


@dataclass(frozen=True)
class SummaryEntry:
    """A recorded unit a PR touches, as the summary shows it."""

    unit: Unit
    familiarity: float
    override: Override | None
    last_kind: ExposureKind | None
    last_at: datetime | None
    changes_since_last: int


def _ago(at: datetime, now: datetime) -> str:
    days = max((now - at).days, 0)
    if days == 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


def _entry_line(entry: SummaryEntry, now: datetime) -> str:
    name = json.dumps(entry.unit.label, ensure_ascii=False)
    line = f"- {name}: {entry.familiarity:.2f}"
    if entry.override is not None:
        return f"{line}, marked {entry.override} by the author"
    if entry.last_kind is not None and entry.last_at is not None:
        line += f", last {_KIND_WORDS[entry.last_kind]} {_ago(entry.last_at, now)}"
    if entry.changes_since_last:
        line += f"; {entry.changes_since_last} lines changed since"
    return line


def render_summary(
    repo: str,
    entries: Sequence[SummaryEntry],
    *,
    unknown: int,
    topics: Sequence[tuple[Unit, float]],
    now: datetime,
) -> str:
    """The knowledge summary for the ContextPack, at most ``SUMMARY_CHARS``: the recorded
    units a PR touches (the author's overrides first, then least familiar first, names
    quoted), how many touched areas have no record, and the author's best-known topics.
    Empty when there is nothing to say."""
    if not entries and not unknown:
        return ""
    # The author's own corrections first, then the least familiar areas.
    ranked = sorted(entries, key=lambda e: (e.override is None, e.familiarity, e.unit.id))
    lines = [f"Familiarity with the code this PR touches in {repo} (0 new, 1 well known):"]
    budget = SUMMARY_CHARS - 200  # room for the closing lines
    shown = 0
    for entry in ranked[:SUMMARY_UNITS]:
        line = _entry_line(entry, now)
        if sum(map(len, lines)) + len(line) + len(lines) > budget:
            break
        lines.append(line)
        shown += 1
    if len(ranked) > shown:
        lines.append(f"- {len(ranked) - shown} more areas not listed")
    if unknown:
        area = "area has" if unknown == 1 else "areas have"
        lines.append(f"{unknown} touched {area} no record: treat them as new to the author.")
    if ranked:
        lines.append(f"Lowest familiarity: {min(e.familiarity for e in ranked):.2f}.")
    if topics:
        best = ", ".join(f"{u.label} {score:.2f}" for u, score in topics[:SUMMARY_TOPICS])
        lines.append(f"Topics the author knows: {best}.")
    return "\n".join(lines)[:SUMMARY_CHARS]
