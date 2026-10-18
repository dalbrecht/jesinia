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
  (no labels) and the theme's issues are linked under it. Only a theme epic still running
  blocks another approval; when an earlier one didn't finish, the next plan reuses the epic it
  created and links only the missing children (IR53), so a second epic is never made.
- **Catalog renames and merges** are followed (IR54): every call resolves the stored tags
  through the catalog's aliases and rewrites the rows to the canonical name, so a renamed tag
  lists, and filters, under its new name (and the old name still finds it). Without the
  catalog the stored names are used as they are.

The catalog is looked up per call (the kb service starts first, but may be stopped): without
it new themes wait (``ThemesUnavailableError``) while reads, removing, publishing and epics
carry on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from jesinia.apps.issues.application.epics import (
    BatchStarter,
    FactsCache,
    ancestors_of,
    facts_of,
)
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
    Key,
    Node,
    NothingToConvertError,
)
from jesinia.apps.issues.domain.schema import label_key
from jesinia.apps.issues.domain.themes import (
    InvalidThemeError,
    NothingToPublishError,
    ThemeCatalogError,
    ThemeEpicClosedError,
    ThemeNearDuplicateError,
    ThemeNotFoundError,
    ThemesUnavailableError,
    has_theme_label,
    is_publishable,
    theme_epic_plan,
    theme_label,
    theme_label_change,
    theme_tag,
)
from jesinia.apps.issues.domain.triage import IssueNotCachedError
from jesinia.services.github.domain.issue import IssueRef
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
    """A theme, its number of issues, and whether its ``theme:<tag>`` fits a GitHub label (a
    longer tag is a local theme only)."""

    tag: str
    count: int
    publishable: bool


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


def _origin(tag: str, repo: str) -> str:
    """The ledger origin of a theme's epic batches: the theme and the epic's repo (IR57)."""
    return f"theme:{tag}:{repo.casefold()}"


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

    # The catalog's names (IR54) -------------------------------------------------------

    async def _names(self) -> dict[str, str]:
        """Each catalog tag and alias, mapped to its canonical tag (tags win over aliases, as
        in the catalog's own ``resolve``). Empty without the catalog: best effort, a failure is
        logged with its cause and the stored names are used."""
        catalog = self._tags()
        if catalog is None:
            return {}
        try:
            infos = await catalog.catalog()
        except (KbError, OSError) as exc:
            log.warning(
                "issues.themes: reading the tag catalog failed (%s: %s); using stored names",
                type(exc).__name__,
                exc,
            )
            return {}
        names = {alias: info.tag for info in infos for alias in info.aliases}
        names.update({info.tag: info.tag for info in infos})
        return names

    async def _reconcile(self) -> dict[str, str]:
        """Rewrite stored themes whose tag the catalog now names otherwise (renamed, or merged
        into another), and answer the catalog's names."""
        names = await self._names()
        for stored, _ in self._store.themes():
            canonical = names.get(stored, stored)
            if canonical != stored:
                self._store.retag(stored, canonical)
        return names

    async def canonical(self, raw: str) -> str:
        """``raw`` as a theme's tag, by its catalog name (an old name of a renamed tag gives
        the new one).

        Raises:
            InvalidThemeError: not a tag.
        """
        return await self._tag(raw)

    async def _tag(self, raw: str) -> str:
        """``raw`` as a tag, by its catalog name, after the stored themes are reconciled.

        Raises:
            InvalidThemeError: not a tag.
        """
        tag = theme_tag(raw)
        names = await self._reconcile()
        return names.get(tag, tag)

    # Reads ------------------------------------------------------------------------------

    async def themes(self) -> list[ThemeSummary]:
        """Every theme with its number of issues, by tag (the column of drop targets)."""
        await self._reconcile()
        return [
            ThemeSummary(tag, count, is_publishable(tag)) for tag, count in self._store.themes()
        ]

    async def issues(self, raw: str) -> list[ThemedIssue]:
        """The theme's issues, by repo and number (the theme as a filter). An old name of a
        renamed tag finds them too.

        Raises:
            InvalidThemeError: not a tag.
            ThemeNotFoundError: no issue carries it.
        """
        found = []
        for repo, n in self._refs(await self._tag(raw)):
            cached = self._cache.cached_issue(repo, n)
            if cached is None:
                found.append(ThemedIssue(repo, n, None, None, None))
                continue
            issue = cached.issue
            found.append(ThemedIssue(issue.repo, n, issue.title, issue.state, issue.url))
        return found

    async def of_issue(self, repo: str, n: int) -> list[str]:
        """The issue's themes, by tag."""
        checked = validate_repo(repo)
        await self._reconcile()
        return self._store.of_issue(checked, n)

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

    async def remove(self, refs: Sequence[Ref], raw: str) -> int:
        """Take the theme off these issues, locally (an Undo of an add too); how many had it.
        The catalog entry stays (IR22).

        Raises:
            InvalidThemeError: not a tag, or no (or too many) issues.
            InvalidRepoError: a repo isn't ``owner/name``.
        """
        self._bounded(refs)
        checked = [(validate_repo(r), n) for r, n in refs]
        return self._store.remove(checked, await self._tag(raw))

    # Publishing (staged, IR4) -----------------------------------------------------------

    async def publish(self, raw: str, repo: str) -> ThemePublish:
        """Stage the theme's label in ``repo``: the setup step creating ``theme:<tag>`` and the
        label addition for each of the theme's cached issues there that lacks it. A step the
        tray already holds is reused, so publishing twice stages nothing more. Nothing reaches
        GitHub until the tray is applied.

        Raises:
            InvalidThemeError: not a tag, or one too long for a GitHub label.
            InvalidRepoError: ``repo`` isn't ``owner/name``.
            ThemeNotFoundError: no issue carries it.
            NothingToPublishError: no cached issue of the theme in ``repo`` lacks the label.
        """
        home = validate_repo(repo).casefold()
        tag = await self._tag(raw)
        label = theme_label(tag)
        lacking: list[CachedIssue] = []
        already: list[Ref] = []
        for ref in self._refs(tag):
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

    async def to_epic(
        self, raw: str, repo: str, digest: str | None = None, *, new_epic: bool = False
    ) -> ConversionPlan:
        """The plan turning the theme into an epic created in ``repo`` (one of its issues'
        repos): its cached issues become the children (those in ``repo`` first, then by repo
        and number), each checked like a conversion's link (another owner's refused, another
        repo warned, one with a parent skipped). When an earlier epic for the theme didn't
        finish, the plan reuses the epic it created and links only the missing children
        (IR53). Once the theme has an epic the plan links under it; a new epic is planned only
        with ``new_epic`` (IR59). With ``digest`` (an approval) the plan must still be the one
        the author saw and must have children.

        Raises:
            InvalidThemeError: not a tag, or ``repo`` isn't one of the theme's repos.
            ThemeNotFoundError: no issue carries it.
            ThemeEpicClosedError: the theme's epic is closed and ``new_epic`` isn't asked.
            ConversionOutdatedError: the plan changed since ``digest``.
            NothingToConvertError: approving a plan with no children.
        """
        home = validate_repo(repo)
        return self._plan(await self._tag(raw), home, digest, new_epic)

    def _plan(self, tag: str, home: str, digest: str | None, new_epic: bool) -> ConversionPlan:
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
        existing, ancestors = (None, ()) if new_epic else self._reusable_epic(tag, home)
        plan = theme_epic_plan(tag, home, members, uncached, existing, ancestors)
        if digest is not None:
            if plan.digest != digest:
                raise ConversionOutdatedError(
                    f"the theme #{tag} or its issues changed since the plan was shown; look at "
                    "it again before approving"
                )
            if not plan.children:
                raise NothingToConvertError(f"the theme #{tag} has no issue left to link")
        return plan

    def _reusable_epic(self, tag: str, home: str) -> tuple[Node | None, tuple[Key, ...]]:
        """The theme's latest epic in ``home``, with its ancestors, or ``None`` when it has
        none yet (ruling IR59): the epic its newest settled batch (complete, or abandoned,
        IR58) made, or linked under, kept in ``theme_epics`` and the ledger after the batch is
        pruned. Issues added later link under it, never under a new epic, unless the author
        asks for one.

        Raises:
            ThemeEpicClosedError: that epic is closed (reopen it, or ask for a new epic).
        """
        epic = self._latest_epic(tag, home)
        if epic is None:
            return None, ()
        found = facts_of(self._cache, *epic)
        if found is not None and not found.is_open:
            raise ThemeEpicClosedError(
                f"the theme #{tag}'s epic {epic[0]}#{epic[1]} is closed; reopen it or ask for a "
                "new epic"
            )
        node = Node(*epic) if found is None else found.node
        return node, ancestors_of(self._cache, *epic)

    def _latest_epic(self, tag: str, home: str) -> tuple[str, int] | None:
        """The epic of the theme's newest batch in ``home`` that has one: recorded with the
        batch (an existing epic linked under), or made by it once settled (from the ledger)."""
        ledger = {row.batch_id: row for row in self._batches.origin_batches(_origin(tag, home))}
        for batch_id, epic in self._store.epic_rows(tag):
            if epic is not None and epic[0].casefold() == home.casefold():
                return epic
            row = ledger.get(batch_id)
            made = None if row is None else row.created.get("epic")
            if row is not None and made is not None and row.state in ("complete", "abandoned"):
                self._store.set_epic(tag, batch_id, *made)
                return made
        # A settled batch the theme's own record missed: the ledger still knows its epic.
        for row in reversed(list(ledger.values())):
            made = row.created.get("epic")
            if made is not None and row.state in ("complete", "abandoned"):
                return made
        return None

    async def start_epic(
        self,
        raw: str,
        repo: str,
        digest: str,
        key: str | None,
        starter: BatchStarter,
        *,
        new_epic: bool = False,
    ) -> AppliedBatch:
        """Approve the epic previewed with ``digest``: one batch through the apply engine, in
        the background (IR45); a replay of ``key`` answers its batch (IR46). Refused while an
        earlier epic batch for the theme (in that repo) is running or didn't finish: it may
        have made the epic, so it is retried or abandoned instead (IR56, IR58). The batch is
        recorded for the theme on a replay too, so a record the first request couldn't write is
        never lost.

        A new epic is made only when the theme has none yet or ``new_epic`` asks for one
        (IR59).

        Raises:
            EarlierBatchError: see above.
            ThemeEpicClosedError: the theme's epic is closed and ``new_epic`` isn't asked.
            Others: as ``to_epic``.
        """
        home = validate_repo(repo)
        tag = await self._tag(raw)
        planned: list[ConversionPlan] = []

        def make() -> ChangeBatch:
            plan = self._plan(tag, home, digest, new_epic)
            planned.append(plan)
            return plan.changes

        started = starter.start_batch(make, "epic", key, _origin(tag, home))
        reused = planned[0].epic if planned else None
        known = None if not isinstance(reused, IssueRef) else (reused.repo, reused.number)
        self._store.record_epic(tag, started.id, self._clock(), known)
        return started
