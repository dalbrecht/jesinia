# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the repo workspace accepts, and the typed errors it raises.

Repo names, SHAs and refs reach git as argv entries, so each is checked against a strict
pattern first: nothing that starts with ``-`` (an option), contains ``..`` (a path or
revision range) or anything but the characters GitHub allows.
"""

from __future__ import annotations

import re

# GitHub owners: alphanumerics and single hyphens, no leading hyphen, at most 39 chars.
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
# A full ref (``refs/heads/main``, ``refs/pull/12/head``): each segment starts with an
# alphanumeric, so no ``-`` options, no ``..`` and no ``:`` refspecs slip through.
_REF = re.compile(r"refs(/[A-Za-z0-9][A-Za-z0-9._-]*)+")


class WorkspaceError(Exception):
    """Base for every repo workspace failure."""


class InvalidRepoError(WorkspaceError):
    """The repository is not a GitHub ``owner/name``."""


class InvalidShaError(WorkspaceError):
    """A commit was not given as a full 40-character lowercase SHA."""


class InvalidRefError(WorkspaceError):
    """A ref to fetch is neither a full ``refs/...`` name nor a full SHA."""


class WorkspacePathError(WorkspaceError):
    """A workspace path resolved outside the workspace root (e.g. through a symlink)."""


class InvalidTreePathError(WorkspaceError):
    """A path inside a commit that is absolute, climbs out with ``..``, or looks like an
    option."""


class MirrorFileTooLargeError(WorkspaceError):
    """A file at a commit is larger than the caller allows; it was not read."""


class GitCommandError(WorkspaceError):
    """A git command exited non-zero; ``stderr`` keeps git's own explanation."""

    def __init__(self, argv: tuple[str, ...], code: int, stderr: str) -> None:
        super().__init__(f"{' '.join(argv)} failed ({code}): {stderr.strip()}")
        self.argv = argv
        self.code = code
        self.stderr = stderr


class GitOutputError(WorkspaceError):
    """git's output did not have the shape the workspace relies on."""


class GitTimeoutError(WorkspaceError):
    """A git command ran past its time limit and was killed."""

    def __init__(self, argv: tuple[str, ...], seconds: float) -> None:
        super().__init__(f"{' '.join(argv)} timed out after {seconds:g}s")
        self.argv = argv
        self.seconds = seconds


class GitUnavailableError(WorkspaceError):
    """The git executable could not be started (missing from PATH, not executable)."""


def parse_repo(repo: str) -> tuple[str, str]:
    """Split ``owner/name`` and lowercase both: GitHub names are case-insensitive, and so is
    the default macOS filesystem, so ``Acme/Widget`` and ``acme/widget`` are one repo.

    Raises:
        InvalidRepoError: anything else, including ``.`` and ``..`` as the name.
    """
    owner, slash, name = repo.partition("/")
    if not slash or not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in {".", ".."}:
        raise InvalidRepoError(f"not a GitHub owner/name: {repo!r}")
    return owner.lower(), name.lower()


def canonical_repo(repo: str) -> str:
    """The one spelling of ``repo`` used for paths, locks and comparisons.

    Raises:
        InvalidRepoError: as ``parse_repo``.
    """
    owner, name = parse_repo(repo)
    return f"{owner}/{name}"


def check_sha(sha: str) -> str:
    """Return ``sha`` if it is a full lowercase SHA-1.

    Raises:
        InvalidShaError: short SHAs, ref names and anything else.
    """
    if not _SHA.fullmatch(sha):
        raise InvalidShaError(f"not a full commit SHA: {sha!r}")
    return sha


def check_ref(ref: str) -> str:
    """Return ``ref`` if it is a full ref name or a full SHA.

    Raises:
        InvalidRefError: short names, options, ranges and refspecs.
    """
    if _SHA.fullmatch(ref) or (_REF.fullmatch(ref) and ".." not in ref):
        return ref
    raise InvalidRefError(f"not a full ref or commit SHA: {ref!r}")


def check_tree_path(path: str) -> str:
    """Return ``path`` if it names a file inside a commit's tree: relative, no empty, ``.``
    or ``..`` parts, and not starting with ``-``.

    Raises:
        InvalidTreePathError: anything else.
    """
    parts = path.split("/")
    if not path or path.startswith("-") or any(part in {"", ".", ".."} for part in parts):
        raise InvalidTreePathError(f"not a path inside a commit: {path!r}")
    return path
