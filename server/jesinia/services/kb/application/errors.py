# SPDX-License-Identifier: AGPL-3.0-or-later
"""KB service errors."""


class KbError(Exception):
    """Base class for KB service errors."""


class KbUnavailable(KbError):
    """No KB path is configured (or it is missing)."""


class PageNotFound(KbError):
    """No page with that id is indexed, or its file is gone."""


class StaleWrite(KbError):
    """The file changed since the writer read it (or no base revision was given)."""


class PathOutsideKb(KbError):
    """The target resolves outside the KB root (``..``, absolute, or a symlink)."""


class ReservedPath(KbError):
    """The target is in a suite-reserved (``Journal``, ``Drafts``) or hidden folder."""


class InvalidAssetName(KbError):
    """An asset file name that is not one plain file name (no folders, no leading dot)."""


class InvalidTag(KbError):
    """The text is not a usable tag (empty, or outside the ``#tag`` grammar)."""


class TagExists(KbError):
    """The tag (or an alias of it) is already in the catalog."""


class NearDuplicate(KbError):
    """The new tag is probably an existing one spelled differently; ``near`` lists them."""

    def __init__(self, tag: str, near: list[str]) -> None:
        super().__init__(f"#{tag} is close to existing tags: {', '.join(near)}")
        self.near = near


class TagNotFound(KbError):
    """The tag is not in the catalog."""
