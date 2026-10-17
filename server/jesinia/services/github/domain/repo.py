# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo names and URL path segments, shared by every GitHub client.

Pure: no I/O. Anything built into a request path from data (a label name, a file path's
parts) goes through ``path_segment``, so it stays exactly one segment and can never be
normalised into a path to another resource: ``..`` in ``/repos/o/r/labels/..`` would reach
``/repos/o/r`` itself.
"""

from __future__ import annotations

import re
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_DOT_SEGMENTS: frozenset[str] = frozenset({".", ".."})


class GitHubReferenceError(ValueError):
    """Base for values that cannot address GitHub safely."""


class InvalidRepoError(GitHubReferenceError):
    """Not a plain ``owner/name``."""


class InvalidPathSegmentError(GitHubReferenceError):
    """An empty or dot-only value where one URL path segment is needed."""


def validate_repo(repo: str) -> str:
    """Return ``repo`` if it is a plain ``owner/name``, so it can't steer a request path."""
    name = repo.partition("/")[2]
    if not _REPO.fullmatch(repo) or name in _DOT_SEGMENTS:
        raise InvalidRepoError(f"repo must be owner/name, got {repo!r}")
    return repo


def path_segment(value: str) -> str:
    """``value`` as exactly one URL path segment.

    Everything outside RFC 3986's unreserved set is percent-encoded (``/``, ``%``, ``?``,
    ``#``, spaces, colons and non-ASCII among it), and so is ``.``, so no segment can read as
    ``.`` or ``..`` after decoding. GitHub decodes the segment back to ``value``.

    Raises:
        InvalidPathSegmentError: ``value`` is empty, ``.`` or ``..``.
    """
    if not value or value in _DOT_SEGMENTS:
        raise InvalidPathSegmentError(f"not a usable path segment: {value!r}")
    return quote(value, safe="").replace(".", "%2E")
