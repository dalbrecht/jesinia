# SPDX-License-Identifier: AGPL-3.0-or-later
"""Themes (spec §5.1, §6.2; plan Task 10; rulings IR4, IR15, IR17, IR22, IR27, IR45, IR46).

A theme is a tag from the KB-wide catalog, kept locally per issue (``issue_themes``):

- **Adding** one is local and immediate, never a ``Change`` (IR15). A new tag goes through the
  catalog's instant, catalog-only check (``TagService.check(tag, advise=False)``: no agent, so
  a drop never waits on a runner), then ``TagService.ensure([tag])``, which resolves an alias or
  adds the tag all-or-nothing, as Notes' Save note does. A near-duplicate is refused and nothing
  is stored.
- **Removing** one (and Undo of an add) takes the local theme off only: the catalog entry that
  ``ensure`` added stays, as it does for a note's tags (IR22).
- **Publishing** one in a repo (``publish``) stages in the pending tray the label setup step
  (``EnsureLabel`` for ``theme:<tag>``) and the label additions for the theme's issues there.
  The write-ahead engine applies them with the tray, with its guards (a fresh read per issue,
  the label read live before each add); the label is created only by the setup step (IR4).
- **Turning one into an epic** (``to_epic``, ``start_epic``) is the Task 8 ``ConversionPlan``
  (IR27), applied as one approved batch in the background (IR45, IR46): the epic is created
  (no labels) and the theme's issues are linked under it.

The catalog is looked up per call (the kb service starts first, but may be stopped): without
it new themes wait (``ThemesUnavailableError``) while reads, removing, publishing and epics
carry on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from jesinia.apps.issues.application.epics import BatchStarter, FactsCache, facts_of
from jesinia.apps.issues.application.labels import Clock
from jesinia.apps.issues.application.pending import PendingService
from jesinia.apps.issues.application.ports import (
    AppliedBatch,
    BatchReads,
    CachedIssue,
    ThemeStore,
    ThemeTags,
)
from jesinia.apps.issues.domain.changes import (
    AddLabel,
    ChangeBatch,
    EnsureLabel,
    PendingChange,
)
from jesinia.apps.issues.domain.epics import (
    ConversionOutdatedError,
    ConversionPlan,
    Facts,
    NothingToConvertError,
)
from jesinia.apps.issues.domain.schema import label_key
from jesinia.apps.issues.domain.themes import (
    InvalidThemeError,
    NothingToPublishError,
    ThemeCatalogError,
    ThemeEpicRunningError,
    ThemeNearDuplicateError,
    ThemeNotFoundError,
    ThemesUnavailableError,
    has_theme_label,
    theme_epic_plan,
    theme_label,
    theme_label_change,
    theme_tag,
)
from jesinia.apps.issues.domain.triage import IssueNotCachedError
from jesinia.services.github.domain.repo import validate_repo
from jesinia.services.kb.application.errors import KbError, NearDuplicate

log = logging.getLogger(__name__)

# How many issues one add or remove may name (a multi-selection drag).
THEME_ISSUES_MAX = 500
SOURCE = "theme"

Ref = tuple[str, int]


@dataclass(frozen=True)
class ThemeAdded:
    """A theme added: its canonical ``tag`` and the issues that didn't have it yet (what an
    Undo removes)."""

    tag: str
    added: tuple[Ref, ...]


@dataclass(frozen=True)
class ThemeSummary:
    tag: str
    count: int


@dataclass(frozen=True)
class ThemedIssue:
    """A theme's issue as the cache has it (``title``, ``state`` and ``url`` are ``None`` while
    it isn't cached)."""

    repo: str
    n: int
    title: str | None
    state: str | None
    url: str | None


@dataclass(frozen=True)
class ThemePublish:
    """What publishing staged in the tray (or found staged already): the label setup step and
    the label additions; ``already`` lists the issues whose cached labels have it."""

    tag: str
    repo: str
    label: str
    ensure: PendingChange
    added: tuple[PendingChange, ...]
    already: tuple[Ref, ...]


class ThemeService:
    """Themes over ``issue_themes``, the issue cache, the tray and the KB tag catalog.

    Args:
        store: the themes (``issues.sqlite``).
        cache: the issue cache.
        pending: the pending-changes tray.
        tags: looks up the KB tag catalog (``None`` while the kb service isn't running).
        clock: the current UTC time.
        batches: stored batches (a theme epic's, for the guard against a second epic).
    """

    def __init__(
        self,
        store: ThemeStore,
        cache: FactsCache,
        pending: PendingService,
        tags: Callable[[], ThemeTags | None],
        clock: Clock,
        *,
        batches: BatchReads,
    ) -> None:
        self._store = store
        self._cache = cache
        self._pending = pending
        self._tags = tags
        self._clock = clock
        self._batches = batches

    # Reads ------------------------------------------------------------------------------

    def themes(self) -> list[ThemeSummary]:
        """Every theme with its number of issues, by tag (the column of drop targets)."""
        return [ThemeSummary(tag, count) for tag, count in self._store.themes()]

    def issues(self, raw: str) -> list[ThemedIssue]:
        """The theme's issues, by repo and number (the theme as a filter).

        Raises:
            InvalidThemeError: not a tag.
            ThemeNotFoundError: no issue carries it.
        """
        found = []
        for repo, n in self._refs(theme_tag(raw)):
            cached = self._cache.cached_issue(repo, n)
            if cached is None:
                found.append(ThemedIssue(repo, n, None, None, None))
                continue
            issue = cached.issue
            found.append(ThemedIssue(issue.repo, n, issue.title, issue.state, issue.url))
        return found

    def of_issue(self, repo: str, n: int) -> list[str]:
        """The issue's themes, by tag."""
        return self._store.of_issue(validate_repo(repo), n)

    def _refs(self, tag: str) -> list[Ref]:
        refs = self._store.issues(tag)
        if not refs:
            raise ThemeNotFoundError(f"no issue has the theme #{tag}")
        return refs

    # Adding and removing (local, IR15) --------------------------------------------------

    def _cached(self, repo: str, n: int) -> CachedIssue:
        found = self._cache.cached_issue(validate_repo(repo), n)
        if found is None:
            raise IssueNotCachedError(f"{repo}#{n} is not in the cache; sync the repo first")
        return found

    @staticmethod
    def _bounded(refs: Sequence[Ref]) -> None:
        if not refs:
            raise InvalidThemeError("name at least one issue")
        if len(refs) > THEME_ISSUES_MAX:
            raise InvalidThemeError(f"at most {THEME_ISSUES_MAX} issues at a time")

    async def add(self, refs: Sequence[Ref], raw: str) -> ThemeAdded:
        """Give the cached issues ``refs`` the theme ``raw``, at once and locally. A tag new to
        the catalog passes its catalog-only near-duplicate check (no agent) and is added to it.

        Raises:
            InvalidThemeError: not a tag, or no (or too many) issues.
            IssueNotCachedError: an issue isn't cached; nothing is stored or catalogued.
            ThemesUnavailableError: the tag catalog isn't available.
            ThemeNearDuplicateError: the tag is close to a catalog tag; nothing is stored.
            ThemeCatalogError: the catalog couldn't be read or written (the cause is chained).
        """
        self._bounded(refs)
        tag = theme_tag(raw)
        issues = list(
            dict.fromkeys((c.issue.repo, c.issue.number) for c in (self._cached(*r) for r in refs))
        )
        catalog = self._tags()
        if catalog is None:
            raise ThemesUnavailableError(
                "the KB tag catalog isn't available (is the kb service running?); "
                "themes can't be added until it is"
            )
        try:
            # Catalog-only: a drop never waits on an agent run.
            checked = await catalog.check(tag, advise=False)
            if not checked.exists and checked.near:
                raise ThemeNearDuplicateError(tag, checked.near)
            (canonical,) = await catalog.ensure([tag])
        except NearDuplicate as exc:  # the catalog changed between the check and the add
            raise ThemeNearDuplicateError(tag, exc.near) from exc
        except (KbError, OSError) as exc:
            raise ThemeCatalogError(
                f"the tag catalog couldn't be updated ({type(exc).__name__}: {exc})"
            ) from exc
        added = self._store.add(issues, canonical, self._clock())
        return ThemeAdded(canonical, tuple(added))

    def remove(self, refs: Sequence[Ref], raw: str) -> int:
        """Take the theme off these issues, locally (an Undo of an add too); how many had it.
        The catalog entry stays (IR22).

        Raises:
            InvalidThemeError: not a tag, or no (or too many) issues.
            InvalidRepoError: a repo isn't ``owner/name``.
        """
        self._bounded(refs)
        tag = theme_tag(raw)
        return self._store.remove([(validate_repo(r), n) for r, n in refs], tag)

    # Publishing (staged, IR4) -----------------------------------------------------------

    def publish(self, raw: str, repo: str) -> ThemePublish:
        """Stage the theme's label in ``repo``: the setup step creating ``theme:<tag>`` and the
        label addition for each of the theme's cached issues there that lacks it. A step the
        tray already holds is reused, so publishing twice stages nothing more. Nothing reaches
        GitHub until the tray is applied.

        Raises:
            InvalidThemeError: not a tag, or one too long for a GitHub label.
            InvalidRepoError: ``repo`` isn't ``owner/name``.
            NothingToPublishError: no cached issue of the theme in ``repo`` lacks the label.
        """
        tag = theme_tag(raw)
        label = theme_label(tag)
        home = validate_repo(repo).casefold()
        lacking: list[CachedIssue] = []
        already: list[Ref] = []
        for ref in self._store.issues(tag):
            cached = self._cache.cached_issue(*ref)
            if cached is None or cached.issue.repo.casefold() != home:
                continue
            if has_theme_label(cached.issue.labels, tag):
                already.append((cached.issue.repo, cached.issue.number))
            else:
                lacking.append(cached)
        if not lacking:
            raise NothingToPublishError(
                f"no cached issue in {repo} still needs {label!r}"
                if already
                else f"no cached issue in {repo} has the theme #{tag}"
            )
        home_repo = lacking[0].issue.repo
        tray = self._pending.pending()
        staged: list[PendingChange] = []
        try:
            ensure = self._staged_ensure(tray, home_repo, label)
            if ensure is None:
                ensure = self._pending.stage_label(theme_label_change(home_repo, tag), SOURCE)
                staged.append(ensure)
            added: list[PendingChange] = []
            for cached in lacking:
                found = self._staged_add(tray, cached, label)
                if found is None:
                    found = self._pending.stage(
                        cached.issue.repo, cached.issue.number, AddLabel(label), SOURCE
                    )
                    staged.append(found)
                added.append(found)
        except BaseException:
            self._unstage(staged)
            raise
        return ThemePublish(tag, home_repo, label, ensure, tuple(added), tuple(already))

    def _unstage(self, staged: Sequence[PendingChange]) -> None:
        """Take back what a failed publish staged, best effort (each failure is logged)."""
        for change in staged:
            try:
                self._pending.unstage(change.id)
            except Exception:
                log.exception("issues.themes: unstaging %s after a failed publish", change.id)

    @staticmethod
    def _staged_ensure(
        tray: Sequence[PendingChange], repo: str, label: str
    ) -> PendingChange | None:
        return next(
            (
                found
                for found in tray
                if isinstance(found.change, EnsureLabel)
                and found.change.repo.casefold() == repo.casefold()
                and label_key(found.change.name) == label_key(label)
            ),
            None,
        )

    @staticmethod
    def _staged_add(
        tray: Sequence[PendingChange], cached: CachedIssue, label: str
    ) -> PendingChange | None:
        key = (cached.issue.repo.casefold(), cached.issue.number)
        return next(
            (
                found
                for found in tray
                if found.key == key
                and isinstance(found.change, AddLabel)
                and label_key(found.change.name) == label_key(label)
            ),
            None,
        )

    # An epic (one approved batch, IR27) -------------------------------------------------

    def to_epic(self, raw: str, repo: str, digest: str | None = None) -> ConversionPlan:
        """The plan turning the theme into an epic created in ``repo`` (one of its issues'
        repos): its cached issues become the children (those in ``repo`` first, then by repo
        and number), each checked
        like a conversion's link (another owner's refused, another repo warned, one with a
        parent skipped). With ``digest`` (an approval) the plan must still be the one the
        author saw and must have children.

        Raises:
            InvalidThemeError: not a tag, or ``repo`` isn't one of the theme's repos.
            ThemeNotFoundError: no issue carries it.
            ConversionOutdatedError: the plan changed since ``digest``.
            NothingToConvertError: approving a plan with no children.
        """
        tag = theme_tag(raw)
        home = validate_repo(repo)
        members: list[Facts] = []
        uncached: list[Ref] = []
        # The epic's own repo first, then the others, each by number.
        refs = sorted(
            self._refs(tag),
            key=lambda r: (r[0].casefold() != home.casefold(), r[0].casefold(), r[1]),
        )
        for ref in refs:
            found = facts_of(self._cache, *ref)
            if found is None:
                uncached.append(ref)
            else:
                members.append(found)
        plan = theme_epic_plan(tag, home, members, uncached)
        if digest is not None:
            if plan.digest != digest:
                raise ConversionOutdatedError(
                    f"the theme #{tag} or its issues changed since the plan was shown; look at "
                    "it again before approving"
                )
            if not plan.children:
                raise NothingToConvertError(f"the theme #{tag} has no issue left to link")
        return plan

    def start_epic(
        self, raw: str, repo: str, digest: str, key: str | None, starter: BatchStarter
    ) -> AppliedBatch:
        """Approve the epic previewed with ``digest``: one batch through the apply engine, in
        the background (IR45); a replay of ``key`` answers its batch (IR46). Refused while an
        earlier epic for the theme runs or didn't finish, which approving again could repeat.

        Raises:
            ThemeEpicRunningError: see above (retry that batch instead).
            Others: as ``to_epic``.
        """
        tag = theme_tag(raw)
        made: list[ChangeBatch] = []

        def make() -> ChangeBatch:
            self._check_earlier(tag)
            made.append(self.to_epic(tag, repo, digest).changes)
            return made[0]

        started = starter.start_batch(make, "epic", key)
        if made:  # a new batch, not a replay: nothing awaited since it was stored
            self._store.record_epic(tag, started.id, self._clock())
        return started

    def _check_earlier(self, tag: str) -> None:
        for batch_id in self._store.epic_batches(tag):
            earlier = self._batches.batch(batch_id)
            if earlier is None:  # pruned with the batches it outlived
                continue
            if earlier.status == "running":
                raise ThemeEpicRunningError(
                    f"an epic for the theme #{tag} is still being made (batch {batch_id}); "
                    "wait for it"
                )
            if not earlier.result.complete:
                raise ThemeEpicRunningError(
                    f"an earlier epic for the theme #{tag} didn't finish; retry batch "
                    f"{batch_id} rather than making a second epic"
                )
