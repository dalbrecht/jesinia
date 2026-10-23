# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed errors for the Zotero integration; every one derives from :class:`ZoteroError`."""

from __future__ import annotations

from typing import Literal

UnavailableState = Literal["not_running", "api_off", "unauthorized"]


class ZoteroError(Exception):
    """Base class for every error raised by the Zotero module."""


class ZoteroUnavailable(ZoteroError):  # noqa: N818 - name fixed by the spec
    """Zotero cannot be reached; ``state`` says why so the UI can guide the user."""

    def __init__(self, state: UnavailableState, detail: str = "") -> None:
        super().__init__(f"Zotero unavailable ({state})" + (f": {detail}" if detail else ""))
        self.state: UnavailableState = state


class StaleVersion(ZoteroError):  # noqa: N818 - name fixed by the spec
    """A write was rejected because the item changed in Zotero since it was read."""


class ForbiddenWrite(ZoteroError):  # noqa: N818 - name fixed by the spec
    """A write targeted a field or item the integration is not allowed to modify."""


class WriteTokenUsed(ZoteroError):  # noqa: N818 - name fixed by the spec
    """Zotero rejected a create because its ``Zotero-Write-Token`` was already used."""


class AuthorizeTooSoon(ZoteroError):  # noqa: N818 - name fixed by the spec
    """Authorize was requested again within a minute; Zotero would show another dialog."""
