# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → RSS: how long unkept posts are retained and where kept posts go."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from jesinia.apps.rss.domain.feed import RssError
from jesinia.services.kb.domain.paths import is_page_candidate, is_reserved

RETENTION_DAYS_MIN = 1
RETENTION_DAYS_MAX = 365
RETENTION_POSTS_MIN = 10
RETENTION_POSTS_MAX = 10_000
SOURCE_FOLDER_MAX = 200
SOURCE_FOLDER_KIND = "source"  # the ``folders.source`` config key
DEFAULT_SOURCE_FOLDER = "Sources"


class InvalidSettings(RssError):  # noqa: N818 - the error code clients see
    """A retention outside 1–365 days, or a folder Jesinia cannot write kept posts to."""


RetentionUnit = Literal["days", "posts"]


@dataclass(frozen=True)
class Retention:
    """A feed's own retention: keep ``value`` days of posts, or at most ``value`` posts."""

    unit: RetentionUnit
    value: int


@dataclass(frozen=True)
class RssSettings:
    retention_days: int
    source_folder: str


def validate_source_folder(raw: str) -> str:
    """``raw`` as a KB folder for kept posts: relative, inside the KB, and not reserved
    (``Journal``, ``Drafts``), hidden or ``_assets``; else ``InvalidSettings``."""
    text = raw.strip()
    folder = PurePosixPath(text.strip("/"))
    if not text.strip("/") or text.startswith("/") or ".." in folder.parts:
        raise InvalidSettings(f"{raw!r} is not a folder inside the KB")
    probe = folder / "x.md"
    if is_reserved(probe) or not is_page_candidate(probe):
        raise InvalidSettings(
            f"{folder}: Jesinia never writes pages in reserved, hidden or _assets folders"
        )
    return folder.as_posix()


def validate_settings(retention_days: int, source_folder: str) -> RssSettings:
    if not RETENTION_DAYS_MIN <= retention_days <= RETENTION_DAYS_MAX:
        raise InvalidSettings(
            f"retention must be {RETENTION_DAYS_MIN} to {RETENTION_DAYS_MAX} days"
        )
    return RssSettings(retention_days, validate_source_folder(source_folder))


def validate_retention(unit: str, value: int) -> Retention:
    """A per-feed retention, if ``value`` is 1 to 365 days or 10 to 10000 posts."""
    if unit == "days":
        if not RETENTION_DAYS_MIN <= value <= RETENTION_DAYS_MAX:
            raise InvalidSettings(
                f"retention must be {RETENTION_DAYS_MIN} to {RETENTION_DAYS_MAX} days"
            )
        return Retention("days", value)
    if unit == "posts":
        if not RETENTION_POSTS_MIN <= value <= RETENTION_POSTS_MAX:
            raise InvalidSettings(
                f"retention must be {RETENTION_POSTS_MIN} to {RETENTION_POSTS_MAX} posts"
            )
        return Retention("posts", value)
    raise InvalidSettings(f"retention unit must be days or posts, not {unit!r}")
