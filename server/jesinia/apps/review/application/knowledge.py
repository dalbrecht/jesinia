# SPDX-License-Identifier: AGPL-3.0-or-later
"""The knowledge model's use cases (spec §6.1, plan Task 14): ingest the author's signals,
keep their corrections, and answer the pipeline: the familiarity a depth is proposed from
and the summary in every agent's ContextPack.

Ingest is a secondary subsystem: best-effort and error-isolated. Each source (finished
rounds, quiz results, KB pages, each repo's history) runs on its own; a failure is logged
with its cause and reported, and never stops another source, a round or a posting. Every
signal is stored once, keyed by its source id (``commit:<repo>:<sha>``, ``round:<id>``,
``quiz:<id>``, ``kb:<page>@<rev>``), so running it again changes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jesinia.apps.review.domain.knowledge import (
    HISTORY_HORIZON,
    Exposure,
    ExposureKind,
    InvalidUnitError,
    LoggedCommit,
    Override,
    SummaryEntry,
    Unit,
    UnitRecord,
    churn_by_prefix,
    code_units,
    distance,
    familiarity,
    last_exposure,
    quiz_kind,
    recency,
    render_summary,
    top_units,
    unit_of_file,
    units_of_file,
)
from jesinia.apps.review.domain.queue import PrRef
from jesinia.apps.review.domain.stacks import RoundNotOfPr
from jesinia.apps.review.domain.workspace import WorkspaceError, canonical_repo
from jesinia.services.kb.domain.tag_catalog import indexed_tags
from jesinia.services.kb.domain.tags import is_valid_tag

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# The commits one read of a repo's history may return (newest first).
COMMIT_LOG_LIMIT = 20_000
# Units GET /knowledge lists at most.
VIEW_LIMIT = 500
# Tags one KB page adds as topics at most.
PAGE_TAGS_MAX = 20


@dataclass(frozen=True)
class CommitLog:
    """Default-branch commits, newest first; ``truncated`` when the limit cut older ones."""

    commits: tuple[LoggedCommit, ...]
    truncated: bool = False


@dataclass(frozen=True)
class PageRev:
    """A KB page's id and revision, and when it last changed."""

    page_id: str
    rev: str
    changed_at: datetime


@dataclass(frozen=True)
class PageContent:
    """A KB page's front matter (``jesinia``) and body: untrusted data, only read."""

    jesinia: Mapping[str, object]
    body: str


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


ExposureRow = tuple[Unit, ExposureKind, datetime]
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
        """Repos whose history was read, or with any exposure or override."""
        ...

    def repo_state(self, repo: str) -> RepoState | None: ...

    def record_commits(
        self,
        repo: str,
        state: RepoState,
        churn: Sequence[ChurnRow],
        mine: Mapping[str, Sequence[ExposureRow]],
    ) -> int:
        """Store a history read and move the repo's state on, in one transaction; ``mine``
        maps each source id to its exposures. Returns how many sources were new."""
        ...

    def add_exposures(self, source: str, rows: Sequence[ExposureRow]) -> bool:
        """Store a source's exposures once; ``False`` when the source was already stored."""
        ...

    def pending_rounds(self) -> list[PendingRound]: ...

    def pending_quizzes(self) -> list[PendingRound]: ...

    def round_pr(self, round_id: int) -> PrRef | None: ...

    def add_quiz(self, round_id: int, right: int, answered: int, at: datetime) -> bool:
        """Keep a round's quiz result; ``False`` when it already has one (the first counts)."""
        ...

    def page_revs(self) -> dict[str, str]:
        """The revision each KB page was last read at."""
        ...

    def record_page(self, page_id: str, rev: str, rows: Sequence[ExposureRow]) -> bool:
        """Store a page revision's exposures and remember the revision as read."""
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
        self, repo: str, tip: str, *, after: str | None, since: datetime | None, limit: int
    ) -> CommitLog: ...

    async def identity_email(self) -> str | None:
        """The local git identity's email (``user.email``), if one is set."""
        ...


class ViewerEmails(Protocol):
    """The signed-in ``gh`` user's emails (``PrClient``)."""

    async def viewer_emails(self) -> list[str]: ...


class KnowledgePages(Protocol):
    """KB pages, read through the KB service."""

    def page_revs(self) -> list[PageRev]: ...

    async def page(self, page_id: str) -> PageContent | None: ...


@dataclass(frozen=True)
class IngestReport:
    """What one ingest added (new sources) and each failure, with its cause."""

    commits: int = 0
    reviews: int = 0
    quizzes: int = 0
    pages: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnitView:
    """One unit as Settings → Review shows it."""

    unit: Unit
    familiarity: float
    override: Override | None
    exposures: Mapping[ExposureKind, int]
    last_exposure: datetime | None
    changes_since_last: int
    distance: float


@dataclass(frozen=True)
class KnowledgeView:
    """The units the model has a record of (or an override for), by id."""

    units: tuple[UnitView, ...]
    total: int
    truncated: bool


@dataclass(frozen=True)
class _Model:
    exposures: Mapping[Unit, Sequence[Exposure]]
    overrides: Mapping[Unit, Override]
    top: tuple[Unit, ...]


def _no_repos() -> Iterable[str]:
    return ()


def _digest(emails: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(emails)).encode()).hexdigest()


def _round_units(repo: str, files: Iterable[str]) -> list[Unit]:
    found: dict[str, Unit] = {}
    for path in files:
        for unit in units_of_file(repo, path):
            found.setdefault(unit.id, unit)
    return list(found.values())


def _page_tags(content: PageContent) -> list[Unit]:
    tags = [t for t in indexed_tags(content.body, content.jesinia) if is_valid_tag(t)]
    return [Unit.tag_unit(tag) for tag in dict.fromkeys(tags)][:PAGE_TAGS_MAX]


class KnowledgeService:
    """The knowledge model over ``store``. Without ``workspace`` no history is read, without
    ``identity`` only the local git identity's email is matched, without ``pages`` no KB
    page is read. ``repos`` lists the repos whose history is read (those with a mirror)."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        clock: Clock,
        workspace: KnowledgeWorkspace | None = None,
        identity: ViewerEmails | None = None,
        pages: KnowledgePages | None = None,
        repos: Callable[[], Iterable[str]] = _no_repos,
    ) -> None:
        self._store = store
        self._clock = clock
        self._workspace = workspace
        self._identity = identity
        self._pages = pages
        self._repos = repos
        self._lock = asyncio.Lock()

    # --- ingest ----------------------------------------------------------------------------

    async def ingest(self) -> IngestReport:
        """Read every source once; never raises (each failure is logged and reported)."""
        async with self._lock:
            errors: list[str] = []
            reviews = self._guard("finished rounds", self._ingest_rounds, errors, 0)
            quizzes = self._guard("quiz results", self._ingest_quizzes, errors, 0)
            pages = await self._guard_async("KB pages", self._ingest_pages, errors)
            commits = await self._guard_async("commits", self._ingest_commits_all, errors)
            self._guard("pruning", self._prune, errors, None)
            return IngestReport(commits, reviews, quizzes, pages, tuple(errors))

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
                continue  # counted once its PR's file list is cached
            rows = [(unit, item.kind, item.at) for unit in _round_units(item.repo, item.files)]
            added += self._store.add_exposures(f"{prefix}:{item.round_id}", rows)
        return added

    async def _ingest_pages(self, errors: list[str]) -> int:
        if self._pages is None:
            return 0
        known = self._store.page_revs()
        added = 0
        for rev in self._pages.page_revs():
            if known.get(rev.page_id) == rev.rev:
                continue
            try:
                content = await self._pages.page(rev.page_id)
            except Exception as exc:  # secondary: one unreadable page never stops the rest
                self._failed(f"KB page {rev.page_id}", exc, errors)
                continue
            units = [] if content is None else list(code_units(content.jesinia))
            if units and content is not None:
                units += _page_tags(content)
            rows: list[ExposureRow] = [(unit, "kb_page", rev.changed_at) for unit in units]
            if self._store.record_page(rev.page_id, rev.rev, rows) and rows:
                added += 1
        return added

    async def _emails(self, workspace: KnowledgeWorkspace) -> set[str]:
        """The author's emails, lowercase: the signed-in ``gh`` user's and the local git
        identity's (whatever the workspace allows)."""
        emails: set[str] = set()
        if self._identity is not None:
            emails |= set(await self._identity.viewer_emails())
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
        state, digest = self._store.repo_state(repo), _digest(emails)
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
        read = await workspace.commit_log(
            repo, tip, after=after, since=since, limit=COMMIT_LOG_LIMIT
        )
        if read.truncated:
            log.warning(
                "knowledge ingest: %s's history was cut at %d commits", repo, COMMIT_LOG_LIMIT
            )
        churn: list[ChurnRow] = []
        mine: dict[str, list[ExposureRow]] = {}
        for commit in read.commits:
            churn += [
                (commit.sha, commit.at, p, n) for p, n in churn_by_prefix(commit.files).items()
            ]
            if commit.email.strip().lower() in emails:
                units = _round_units(repo, (path for path, _ in commit.files))
                mine[f"commit:{repo}:{commit.sha}"] = [(u, "commit", commit.at) for u in units]
        return self._store.record_commits(repo, RepoState(tip, digest), churn, mine)

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
        quiz_kind(right, answered)  # validates
        return self._store.add_quiz(round_id, right, answered, self._clock())

    # --- reads ------------------------------------------------------------------------------

    def _model(self) -> _Model:
        exposures = self._store.exposures()
        now = self._clock()
        scores = {unit: recency(seen, now) for unit, seen in exposures.items()}
        return _Model(exposures, self._store.overrides(), top_units(scores))

    def _view(self, model: _Model, unit: Unit) -> UnitView:
        seen = tuple(model.exposures.get(unit, ()))
        last = last_exposure(seen)
        changes = 0 if last is None else self._store.changes_since(unit, last)
        far = distance(unit, model.top)
        record = UnitRecord(unit, seen, model.overrides.get(unit))
        counts: dict[ExposureKind, int] = {}
        for exposure in seen:
            counts[exposure.kind] = counts.get(exposure.kind, 0) + 1
        score = familiarity(record, self._clock(), changes, far)
        return UnitView(unit, score, record.override, counts, last, changes, far)

    def _touched(self, repo: str, paths: Sequence[str]) -> list[Unit] | None:
        """The deepest unit of each touched path, or ``None`` when nothing can be said yet:
        no paths, or a repo the model has no record of (its history unread)."""
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

    def familiarity(self, repo: str, paths: Sequence[str]) -> float | None:
        """The lowest familiarity over the units ``paths`` touch (what ``propose_depth``
        reads); ``None`` when unknown (see ``_touched``)."""
        units = self._touched(repo, paths)
        if units is None:
            return None
        model = self._model()
        return min(self._view(model, unit).familiarity for unit in units)

    def summary(self, repo: str, paths: Sequence[str]) -> str:
        """The knowledge summary for the ContextPack (bounded; see ``render_summary``). Only
        units with a record are named, so a PR's own folder names never enter it."""
        units = self._touched(repo, paths)
        if units is None:
            return ""
        model = self._model()
        entries: list[SummaryEntry] = []
        unknown = 0
        for unit in units:
            if unit not in model.exposures and unit not in model.overrides:
                unknown += 1
                continue
            view = self._view(model, unit)
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
        return render_summary(
            canonical_repo(repo), entries, unknown=unknown, topics=topics, now=self._clock()
        )

    def view(self, repo: str | None = None, limit: int = VIEW_LIMIT) -> KnowledgeView:
        """Every unit with a record or an override, by id (only ``repo``'s path units when
        given), at most ``limit`` of them.

        Raises:
            InvalidUnitError: ``repo`` isn't an owner/name.
        """
        model = self._model()
        units = sorted({*model.exposures, *model.overrides}, key=lambda u: u.id)
        if repo is not None:
            wanted = Unit.path_unit(repo, "").repo
            units = [u for u in units if u.repo == wanted]
        shown = tuple(self._view(model, unit) for unit in units[:limit])
        return KnowledgeView(shown, len(units), len(units) > limit)

    def set_override(self, unit: Unit, override: Override | None) -> UnitView:
        """Mark ``unit`` known or unknown (``None`` clears it); persisted at once."""
        self._store.set_override(unit, override, self._clock())
        return self._view(self._model(), unit)


__all__ = [
    "COMMIT_LOG_LIMIT",
    "CommitLog",
    "IngestReport",
    "KnowledgePages",
    "KnowledgeService",
    "KnowledgeStore",
    "KnowledgeView",
    "KnowledgeWorkspace",
    "PageContent",
    "PageRev",
    "PendingRound",
    "RepoState",
    "UnitView",
    "ViewerEmails",
]
