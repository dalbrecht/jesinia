# SPDX-License-Identifier: AGPL-3.0-or-later
"""Theme rules (spec §5.1, §6.2; plan Task 10; rulings IR4, IR15, IR17, IR22, IR27, IR49). Pure.

A **theme** is a tag from the KB-wide catalog (RSS spec §5), kept locally per issue, so the
same ``#tags`` that group notes and kept posts group issues. Adding or removing one is local
and immediate, never a ``Change`` (IR15). It can be:

- **published** as the GitHub label ``theme:<tag>`` (``theme:`` is a free-form prefix, like
  ``review:`` and ``campaign:``): a label setup step (``theme_label_change``) and the label
  additions, staged in the pending tray. Jesinia never creates the label any other way (IR4).
- **turned into an epic** (``theme_epic_plan``): one approved batch creating the epic (no
  labels; its children make it one) and linking the tag's issues under it, planned like a
  checklist conversion (IR27). Another repo of the same owner gets the "won't fit one PR"
  warning; another owner's issue is refused (GitHub's sub-issues need the same owner, IR17).
  The tag reaches GitHub only as code (IR49).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace

from jesinia.apps.issues.domain.changes import EnsureLabel, InvalidChangeError
from jesinia.apps.issues.domain.epics import (
    ConversionPlan,
    Facts,
    Key,
    Node,
    SkippedItem,
    existing_epic_plan,
    inline_code,
    new_epic_plan,
)
from jesinia.apps.issues.domain.schema import label_key
from jesinia.services.github.domain.issue import TITLE_LIMIT
from jesinia.services.kb.domain.tags import is_valid_tag, normalise_tag

THEME_PREFIX = "theme:"
# One colour for every theme label, apart from the schema's dimension colours.
THEME_COLOR = "fbca04"
EPIC_TITLE_PREFIX = "Theme: "
NOT_CACHED = "not in the cache; sync its repo first"


class ThemeDomainError(ValueError):
    """Base for theme refusals."""


class InvalidThemeError(ThemeDomainError):
    """Not a usable theme: not a tag, a label GitHub can't hold, or a repo the theme lacks."""


class ThemeNotFoundError(ThemeDomainError):
    """No issue carries this theme."""


class ThemeNearDuplicateError(ThemeDomainError):
    """The new tag is probably a catalog tag spelled differently; ``near`` lists those (the
    catalog's instant, catalog-only check: no agent is asked)."""

    def __init__(self, tag: str, near: Sequence[str]) -> None:
        self.tag = tag
        self.near = list(near)
        super().__init__(
            f"#{tag} is close to existing tags: {', '.join(f'#{n}' for n in self.near)}; "
            "use one of those, or add the tag to the catalog first"
        )


class NothingToPublishError(ThemeDomainError):
    """No issue in that repo still needs the theme's label."""


class ThemeEpicClosedError(ThemeDomainError):
    """The theme's latest epic is closed: reopen it, or ask for a new epic explicitly (ruling
    IR59); a new one is never made implicitly. ``epic`` is that epic, so a client can link it."""

    def __init__(self, epic: tuple[str, int], message: str) -> None:
        self.epic = epic
        super().__init__(message)


class ThemesUnavailableError(ThemeDomainError):
    """The KB tag catalog isn't available (the kb service isn't running): new themes wait."""


class ThemeCatalogError(ThemeDomainError):
    """The tag catalog couldn't be read or written; the cause is chained and in the message."""


def theme_tag(raw: str) -> str:
    """``raw`` in the catalog's canonical form (no ``#``, case-folded, spaces as ``-``).

    Raises:
        InvalidThemeError: it isn't a ``#tag``.
    """
    tag = normalise_tag(raw)
    if not is_valid_tag(tag):
        raise InvalidThemeError(
            f"{raw!r} is not a valid tag (a letter or digit, then letters, digits, -)"
        )
    return tag


def theme_label(tag: str) -> str:
    """The GitHub label a published theme carries.

    Raises:
        InvalidThemeError: the label would be too long (or otherwise unusable) for GitHub.
    """
    try:
        return theme_label_change("o/r", tag).name
    except InvalidChangeError as exc:
        raise InvalidThemeError(f"#{tag} can't be a GitHub label: {exc}") from exc


def theme_label_change(repo: str, tag: str) -> EnsureLabel:
    """The label setup step creating ``theme:<tag>`` in ``repo``. A free-form label records no
    schema entry: ``theme:`` is already an allowed prefix.

    Raises:
        InvalidChangeError: the repo or the label isn't valid for GitHub.
    """
    return EnsureLabel(repo, f"{THEME_PREFIX}{tag}", THEME_COLOR, f"Theme #{tag} (Jesinia)")


def is_publishable(tag: str) -> bool:
    """Whether the theme's ``theme:<tag>`` fits a GitHub label. A longer tag still works as a
    local theme and filter; only publishing it is refused."""
    try:
        theme_label(tag)
    except InvalidThemeError:
        return False
    return True


def has_theme_label(labels: Iterable[str], tag: str) -> bool:
    """Whether an issue's ``labels`` already include the theme's label."""
    wanted = label_key(f"{THEME_PREFIX}{tag}")
    return any(label_key(label) == wanted for label in labels)


def theme_epic_plan(
    tag: str,
    repo: str,
    members: Sequence[Facts],
    uncached: Sequence[tuple[str, int]],
    existing: Node | None = None,
    ancestors: Sequence[Key] = (),
) -> ConversionPlan:
    """The plan turning the theme into an epic created in ``repo``: its cached issues
    (``members``, in order) become children unless ``new_epic_plan`` skips them (another
    owner's, under a parent already, listed twice); its ``uncached`` ones are skipped. With
    ``existing`` (the epic an unfinished batch for the theme created, whose ``ancestors`` are
    given, ruling IR53) nothing is created: only the children it lacks are linked under it.

    Raises:
        InvalidThemeError: ``repo`` isn't one of the theme's issues' repos.
    """
    repos = {r.casefold(): r for r in [*(f.repo for f in members), *(r for r, _ in uncached)]}
    home = repos.get(repo.casefold())
    if home is None:
        raise InvalidThemeError(
            f"the epic goes in one of the theme's repos: {', '.join(sorted(repos.values()))}"
        )
    if existing is not None:
        plan = existing_epic_plan(existing, ancestors, members)
    else:
        title = EPIC_TITLE_PREFIX + inline_code(tag, limit=TITLE_LIMIT - len(EPIC_TITLE_PREFIX))
        body = f"Grouped by Jesinia from the theme {inline_code(f'#{tag}')}."
        plan = new_epic_plan(home, title, body, members)
    missing = tuple(SkippedItem(f"{r}#{n}", NOT_CACHED) for r, n in uncached)
    return replace(plan, skipped=(*plan.skipped, *missing))
