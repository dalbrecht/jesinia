# SPDX-License-Identifier: AGPL-3.0-or-later
"""The knowledge model's use cases (spec §6.1, plan Task 14): ingest the author's signals,
keep their corrections, and answer the pipeline: the familiarity a depth is proposed from
and the summary in every agent's ContextPack.

Ingest is a secondary subsystem: best-effort and error-isolated. Each source (finished
rounds, quiz results, each repo's history) runs on its own; a failure is logged
with its cause and reported, and never stops another source, a round or a posting. Every
signal is stored once, keyed by its source id (``commit:<repo>:<sha>``, ``round:<id>``,
``quiz:<id>``), so running it again changes nothing. KB pages are left out (R84): a page
in the KB isn't necessarily the author's, so it isn't evidence of what they know.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from jesinia.apps.review.application.errors import KnowledgeSourceError
from jesinia.apps.review.domain.knowledge import (
    HISTORY_HORIZON,
    Exposure,
    ExposureKind,
    InvalidUnitError,
    KnowledgeInputError,
    LoggedCommit,
    Override,
    SummaryEntry,
    Unit,
    UnitRecord,
    churn_by_prefix,
    distance,
    familiarity,
    has_evidence,
    last_exposure,
    nearest,
    quiz_kind,
    recency,
    render_summary,
    scored_exposures,
    top_units,
    unit_of_file,
    units_of_file,
)
from jesinia.apps.review.domain.queue import PrRef
from jesinia.apps.review.domain.stacks import RoundNotOfPr
from jesinia.apps.review.domain.workspace import WorkspaceError, canonical_repo

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# The commits one read of a repo's history may return (newest first).
COMMIT_LOG_LIMIT = 20_000
# Units GET /knowledge lists at most.
VIEW_LIMIT = 500
# Pages of ``COMMIT_LOG_LIMIT`` commits one history read goes through (M2).
LOG_PAGES = 10
# Churn rows stored per write, yielding to the event loop between writes (M4).
CHURN_CHUNK = 5_000
# A round from before rounds kept their paths, with no cached file list left, is settled
# with nothing to count after this long, rather than asked about at every ingest (N7).
LEGACY_ROUND_GRACE = timedelta(days=30)


@dataclass(frozen=True)
class CommitLog:
    """Default-branch commits, newest first; ``truncated`` when the limit cut older ones."""

    commits: tuple[LoggedCommit, ...]
    truncated: bool = False


@dataclass(frozen=True)
class RepoState:
    """How far a repo's history was read: the default-branch tip, and a digest of the emails
    the author's commits were matched against then."""

    tip: str
    emails: str


@dataclass(frozen=True)
class PendingRound:
    """A round the author finished (posted or marked done) not yet counted. ``files`` is
    ``None`` while its PR's file list isn't cached."""

    round_id: int
    repo: str
    kind: ExposureKind
    at: datetime
    files: tuple[str, ...] | None


# A unit, the signal's kind and time, and whether it touched the unit's own files (I2).
ExposureRow = tuple[Unit, ExposureKind, datetime, bool]
ChurnRow = tuple[str, datetime, str, int]  # sha, at, unit path, changed lines


class KnowledgeStore(Protocol):
    """The model's records in ``review.sqlite``."""

    def exposures(self) -> dict[Unit, list[Exposure]]: ...

    def overrides(self) -> dict[Unit, Override]: ...

    def set_override(self, unit: Unit, override: Override | None, at: datetime) -> None: ...

    def changes_since(self, unit: Unit, since: datetime) -> int:
        """Lines changed in a path unit after ``since`` (0 for a tag)."""
        ...

    def known_repos(self) -> set[str]:
        """Repos whose history was read, or with an override (M5)."""
        ...

    def repo_state(self, repo: str) -> RepoState | None: ...

    def add_churn(self, repo: str, churn: Sequence[ChurnRow]) -> None:
        """Store changed lines per commit and unit path (each row once)."""
        ...

    def record_commits(
        self, repo: str, state: RepoState, mine: Mapping[str, Sequence[ExposureRow]]
    ) -> int:
        """Store the author's commits and move the repo's state on, in one transaction;
        ``mine`` maps each source id to its exposures. Returns how many sources were new."""
        ...

    def add_exposures(self, source: str, rows: Sequence[ExposureRow]) -> bool:
        """Store a source's exposures once; ``False`` when the source was already stored."""
        ...

    def pending_rounds(self) -> list[PendingRound]: ...

    def pending_quizzes(self) -> list[PendingRound]: ...

    def round_pr(self, round_id: int) -> PrRef | None: ...

    def quiz_questions(self, round_id: int) -> int | None:
        """How many questions the round's quiz has (``None`` without one)."""
        ...

    def add_quiz(
        self, round_id: int, right: int, answered: int, questions: int | None, at: datetime
    ) -> bool:
        """Keep a round's quiz result; ``False`` when it already has one (the first counts)."""
        ...

    def prune(self, before: datetime) -> None:
        """Forget churn and exposures older than ``before`` (their weight is negligible)."""
        ...


class KnowledgeWorkspace(Protocol):
    """The repo workspace's reads for ingest (``GitWorkspace``)."""

    def has_mirror(self, repo: str) -> bool: ...

    async def default_branch_head(self, repo: str) -> str: ...

    async def is_ancestor(self, repo: str, ancestor: str, descendant: str) -> bool: ...

    async def commit_log(
        self,
        repo: str,
        tip: str,
        *,
        after: str | None,
        since: datetime | None,
        limit: int,
        skip: int = 0,
    ) -> CommitLog:
        """Commits reachable from ``tip`` (the default branch's: R80) only, newest first."""
        ...

    async def identity_email(self) -> str | None:
        """The local git identity's email (``user.email``), if one is set."""
        ...


class ViewerEmails(Protocol):
    """The signed-in ``gh`` user's emails (``PrClient``)."""

    async def viewer_emails(self) -> list[str]: ...


@dataclass(frozen=True)
class IngestReport:
    """What one ingest added (new sources) and each failure, with its cause."""

    commits: int = 0
    reviews: int = 0
    quizzes: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnitView:
    """One unit as Settings → Review shows it."""

    unit: Unit
    familiarity: float
    override: Override | None  # the one in force: the unit's own or its nearest folder's
    exposures: Mapping[ExposureKind, int]
    last_exposure: datetime | None
    changes_since_last: int
    distance: float  # applied only without evidence of its own (R82), else 0
    override_unit: Unit | None = None  # where ``override`` is set


@dataclass(frozen=True)
class KnowledgeView:
    """The units the model has a record of (or an override for), by id."""

    units: tuple[UnitView, ...]
    total: int
    truncated: bool  # more units follow this page
    offset: int = 0


@dataclass(frozen=True)
class _Model:
    exposures: Mapping[Unit, Sequence[Exposure]]
    overrides: Mapping[Unit, Override]
    scores: Mapping[Unit, float]
    top: tuple[Unit, ...]

    def override_of(self, unit: Unit) -> tuple[Override, Unit] | None:
        """The nearest override on the unit or a folder above it (I2), and where it is."""
        for candidate in unit.lineage:
            if (found := self.overrides.get(candidate)) is not None:
                return found, candidate
        return None


def _view_order(unit: Unit) -> tuple[bool, str, list[str], str]:
    """Folders by repo, each parent before its children; the topics after."""
    parts = unit.path.split("/") if unit.path else []
    return (unit.tag is not None, unit.repo or "", parts, unit.tag or "")


def _no_repos() -> Iterable[str]:
    return ()


def _digest(emails: Iterable[str], key: bytes) -> str:
    """The emails as a keyed digest (M3): stored only to notice a change, it can't confirm a
    guessed address without the install's key."""
    message = "\n".join(sorted(emails)).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _round_units(repo: str, files: Iterable[str]) -> list[tuple[Unit, bool]]:
    """Every unit the files expose, each marked direct when it is a file's own (deepest)
    unit (I2)."""
    found: dict[str, tuple[Unit, bool]] = {}
    for path in files:
        units = units_of_file(repo, path)
        for unit in units:
            direct = unit == units[-1] or found.get(unit.id, (unit, False))[1]
            found[unit.id] = (unit, direct)
    return list(found.values())


class KnowledgeService:
    """The knowledge model over ``store``. Without ``workspace`` no history is read, without
    ``identity`` only the local git identity's email is matched. ``repos`` lists the repos
    whose history is read (those with a mirror)."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        clock: Clock,
        workspace: KnowledgeWorkspace | None = None,
        identity: ViewerEmails | None = None,
        repos: Callable[[], Iterable[str]] = _no_repos,
        email_key: bytes = b"",
    ) -> None:
        self._store = store
        self._clock = clock
        self._workspace = workspace
        self._identity = identity
        self._repos = repos
        self._email_key = email_key
        self._lock = asyncio.Lock()

    # --- ingest ----------------------------------------------------------------------------

    async def ingest(self) -> IngestReport:
        """Read every source once; never raises (each failure is logged and reported)."""
        async with self._lock:
            errors: list[str] = []
            reviews = self._guard("finished rounds", self._ingest_rounds, errors, 0)
            quizzes = self._guard("quiz results", self._ingest_quizzes, errors, 0)
            commits = await self._guard_async("commits", self._ingest_commits_all, errors)
            self._guard("pruning", self._prune, errors, None)
            return IngestReport(commits, reviews, quizzes, tuple(errors))

    def _guard[T](self, what: str, step: Callable[[], T], errors: list[str], fallback: T) -> T:
        try:
            return step()
        except Exception as exc:  # secondary: one source's failure never stops the rest
            self._failed(what, exc, errors)
            return fallback

    async def _guard_async(
        self, what: str, step: Callable[[list[str]], Awaitable[int]], errors: list[str]
    ) -> int:
        try:
            return await step(errors)
        except Exception as exc:  # secondary: one source's failure never stops the rest
            self._failed(what, exc, errors)
            return 0

    @staticmethod
    def _failed(what: str, exc: BaseException, errors: list[str]) -> None:
        log.warning("knowledge ingest: %s failed: %s", what, exc, exc_info=exc)
        errors.append(f"{what}: {type(exc).__name__}: {exc}")

    def _ingest_rounds(self) -> int:
        return self._ingest_finished(self._store.pending_rounds(), "round")

    def _ingest_quizzes(self) -> int:
        return self._ingest_finished(self._store.pending_quizzes(), "quiz")

    def _ingest_finished(self, pending: Sequence[PendingRound], prefix: str) -> int:
        added = 0
        for item in pending:
            if item.files is None:
                # Counted once its PR's file list is cached; settled empty when it never is.
                if self._clock() - item.at > LEGACY_ROUND_GRACE:
                    self._store.add_exposures(f"{prefix}:{item.round_id}", [])
                continue
            rows: list[ExposureRow] = [
                (unit, item.kind, item.at, direct)
                for unit, direct in _round_units(item.repo, item.files)
            ]
            added += self._store.add_exposures(f"{prefix}:{item.round_id}", rows)
        return added

    async def _emails(self, workspace: KnowledgeWorkspace) -> set[str]:
        """The author's emails, lowercase: the signed-in ``gh`` user's and the local git
        identity's (whatever the workspace allows)."""
        emails: set[str] = set()
        if self._identity is not None:
            try:
                emails |= set(await self._identity.viewer_emails())
            except Exception as exc:  # any adapter failure: typed, with its cause
                raise KnowledgeSourceError(
                    f"the signed-in user's emails couldn't be read: {exc}"
                ) from exc
        if local := await workspace.identity_email():
            emails.add(local)
        return {email.strip().lower() for email in emails if email.strip()}

    async def _ingest_commits_all(self, errors: list[str]) -> int:
        workspace = self._workspace
        if workspace is None:
            return 0
        repos = sorted({canonical_repo(r) for r in self._repos()})
        repos = [r for r in repos if workspace.has_mirror(r)]
        if not repos:
            return 0
        emails = await self._emails(workspace)
        added = 0
        for repo in repos:
            try:
                added += await self._ingest_commits(workspace, repo, emails)
            except Exception as exc:  # secondary: one repo's failure never stops the rest
                self._failed(f"commits of {repo}", exc, errors)
        return added

    async def _ingest_commits(
        self, workspace: KnowledgeWorkspace, repo: str, emails: set[str]
    ) -> int:
        tip = await workspace.default_branch_head(repo)
        state, digest = self._store.repo_state(repo), _digest(emails, self._email_key)
        if state is not None and state.tip == tip and state.emails == digest:
            return 0
        after: str | None = None
        since: datetime | None = None
        # The same emails and a descendant tip: only the new commits. Otherwise (a first
        # read, a force-push, other emails) the horizon again; stored keys make it safe.
        if (
            state is not None
            and state.emails == digest
            and await self._descends(workspace, repo, state.tip, tip)
        ):
            after = state.tip
        else:
            since = self._clock() - HISTORY_HORIZON
        commits = await self._read_history(workspace, repo, tip, after, since)
        churn: list[ChurnRow] = []
        mine: dict[str, list[ExposureRow]] = {}
        read_at = self._clock()
        for commit in commits:
            if commit.email.strip().lower() in emails:
                # The author's own change: an exposure, never "changed since" their look.
                units = _round_units(repo, (path for path, _ in commit.files))
                mine[f"commit:{repo}:{commit.sha}"] = [
                    (u, "commit", commit.at, direct) for u, direct in units
                ]
                continue
            # When the code landed counts for "changed since" (M3). Dates are the commit's
            # to claim (backdated, or a merge's commits keep theirs): a commit read
            # incrementally landed since the last read, so it counts as of now (I3).
            changed = commit.changed_at if after is None else max(commit.changed_at, read_at)
            churn += [(commit.sha, changed, p, n) for p, n in churn_by_prefix(commit.files).items()]
        for start in range(0, len(churn), CHURN_CHUNK):
            self._store.add_churn(repo, churn[start : start + CHURN_CHUNK])
            await asyncio.sleep(0)  # a first read can be large: let the loop breathe
        return self._store.record_commits(repo, RepoState(tip, digest), mine)

    @staticmethod
    async def _read_history(
        workspace: KnowledgeWorkspace,
        repo: str,
        tip: str,
        after: str | None,
        since: datetime | None,
    ) -> list[LoggedCommit]:
        """Every commit of the range, page by page (M2): a cut read goes on past what it
        returned, up to ``LOG_PAGES`` pages; past that it is logged."""
        commits: list[LoggedCommit] = []
        for _ in range(LOG_PAGES):
            read = await workspace.commit_log(
                repo, tip, after=after, since=since, limit=COMMIT_LOG_LIMIT, skip=len(commits)
            )
            commits += read.commits
            if read.truncated and not read.commits:
                log.warning(
                    "knowledge ingest: a commit of %s is over the read limit; its history "
                    "from there is not read",
                    repo,
                )
                return commits
            if not read.truncated:
                return commits
        log.warning("knowledge ingest: %s's history was cut at %d commits", repo, len(commits))
        return commits

    @staticmethod
    async def _descends(workspace: KnowledgeWorkspace, repo: str, old: str, new: str) -> bool:
        """Whether ``new`` contains ``old``; an old tip git no longer has counts as no."""
        try:
            return await workspace.is_ancestor(repo, old, new)
        except WorkspaceError as exc:
            log.info("knowledge ingest: %s's last tip %s is gone (%s); reading the horizon",
                     repo, old[:12], exc)  # fmt: skip
            return False

    def _prune(self) -> None:
        self._store.prune(self._clock() - HISTORY_HORIZON)

    # --- quiz results -----------------------------------------------------------------------

    def record_quiz(self, ref: PrRef, round_id: int, *, right: int, answered: int) -> bool:
        """Keep a round's quiz result for the next ingest; ``False`` when the round already
        has one (the first result counts).

        Raises:
            RoundNotOfPr: the round isn't one of ``ref``'s.
            KnowledgeInputError: counts no quiz can produce.
        """
        if self._store.round_pr(round_id) != ref:
            raise RoundNotOfPr(ref.number, round_id)
        questions = self._store.quiz_questions(round_id)
        quiz_kind(right, answered, questions)  # validates
        return self._store.add_quiz(round_id, right, answered, questions, self._clock())

    # --- reads ------------------------------------------------------------------------------

    def _model(self) -> _Model:
        exposures = self._store.exposures()
        now = self._clock()
        scores = {unit: recency(seen, now) for unit, seen in exposures.items()}
        return _Model(exposures, self._store.overrides(), scores, top_units(scores))

    def _view(self, model: _Model, unit: Unit) -> UnitView:
        seen = tuple(model.exposures.get(unit, ()))
        last = last_exposure(scored_exposures(seen))
        changes = 0 if last is None else self._store.changes_since(unit, last)
        override = model.override_of(unit)
        # Distance is a prior for a unit with no evidence of its own (R82, I2).
        far, prior = 0.0, 0.0
        if not has_evidence(seen):
            far = distance(unit, model.top)
            closest = nearest(unit, model.top)
            prior = 0.0 if closest is None else model.scores[closest]
        record = UnitRecord(unit, seen, None if override is None else override[0])
        counts: dict[ExposureKind, int] = {}
        for exposure in seen:
            counts[exposure.kind] = counts.get(exposure.kind, 0) + 1
        score = familiarity(record, self._clock(), changes, far, prior=prior)
        source = None if override is None else override[1]
        return UnitView(unit, score, record.override, counts, last, changes, far, source)

    def _touched(self, repo: str, paths: Sequence[str]) -> list[Unit] | None:
        """The deepest unit of each touched path, or ``None`` when nothing can be said yet:
        no paths, or a repo whose history was never read and has no override (M5)."""
        if not paths:
            return None
        try:
            canonical = canonical_repo(repo)
            units = [unit_of_file(canonical, path) for path in paths]
        except (InvalidUnitError, WorkspaceError):
            return None
        if canonical not in self._store.known_repos():
            return None
        return list({unit.id: unit for unit in units}.values())

    def assess(self, repo: str, paths: Sequence[str]) -> tuple[float | None, str]:
        """The lowest familiarity over the units ``paths`` touch (what ``propose_depth``
        reads; ``None`` when unknown, see ``_touched``) and the knowledge summary for the
        ContextPack, from one read of the model (M4)."""
        units = self._touched(repo, paths)
        if units is None:
            return None, ""
        model = self._model()
        views = [self._view(model, unit) for unit in units]
        lowest = min(view.familiarity for view in views)
        return lowest, self._summary(model, canonical_repo(repo), views)

    def summary(self, repo: str, paths: Sequence[str]) -> str:
        """The knowledge summary (see ``assess`` and ``render_summary``)."""
        return self.assess(repo, paths)[1]

    def _summary(self, model: _Model, repo: str, views: Sequence[UnitView]) -> str:
        """Names only units with a record or an override in force, as ``summary_label``
        shows them (R79); the rest are counted."""
        entries: list[SummaryEntry] = []
        unknown = 0
        for view in views:
            unit = view.unit
            if unit not in model.exposures and view.override is None:
                unknown += 1
                continue
            newest = max(model.exposures.get(unit, ()), key=lambda e: e.at, default=None)
            entries.append(
                SummaryEntry(
                    unit,
                    view.familiarity,
                    view.override,
                    None if newest is None else newest.kind,
                    None if newest is None else newest.at,
                    view.changes_since_last,
                )
            )
        tags = {u for u in (*model.exposures, *model.overrides) if u.kind == "tag"}
        scored = [(u, self._view(model, u).familiarity) for u in tags]
        topics = sorted((t for t in scored if t[1] > 0), key=lambda t: (-t[1], t[0].id))
        return render_summary(repo, entries, unknown=unknown, topics=topics, now=self._clock())

    def view(
        self, repo: str | None = None, limit: int = VIEW_LIMIT, offset: int = 0
    ) -> KnowledgeView:
        """Every unit with a record or an override, by id: each repo's folders together,
        parents before children, then the topics (only ``repo``'s path units when given).
        One page of at most ``limit`` from ``offset``; ``truncated`` when more follow (I4).

        Raises:
            InvalidUnitError: ``repo`` isn't an owner/name.
            KnowledgeInputError: a negative offset or a limit below 1.
        """
        if offset < 0 or limit < 1:
            raise KnowledgeInputError(f"not a page: offset {offset}, limit {limit}")
        model = self._model()
        units = sorted({*model.exposures, *model.overrides}, key=_view_order)
        if repo is not None:
            wanted = Unit.path_unit(repo, "").repo
            units = [u for u in units if u.repo == wanted]
        page = units[offset : offset + limit]
        shown = tuple(self._view(model, unit) for unit in page)
        return KnowledgeView(shown, len(units), offset + len(page) < len(units), offset)

    def set_override(self, unit: Unit, override: Override | None) -> UnitView:
        """Mark ``unit`` known or unknown (``None`` clears it); persisted at once."""
        self._store.set_override(unit, override, self._clock())
        return self._view(self._model(), unit)


__all__ = [
    "COMMIT_LOG_LIMIT",
    "LEGACY_ROUND_GRACE",
    "CommitLog",
    "IngestReport",
    "KnowledgeService",
    "KnowledgeStore",
    "KnowledgeView",
    "KnowledgeWorkspace",
    "PendingRound",
    "RepoState",
    "UnitView",
    "ViewerEmails",
]
