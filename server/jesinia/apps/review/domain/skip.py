# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which files a review may skip without reading them, and why (spec §3.1, coverage).

One classifier serves both the repo tools (which refuse to read these files) and walkthrough
coverage (which lists them under "Not walked through"), so the two never disagree. It works
from the path alone, plus whether the content is binary; it never reads files itself.
"""

from __future__ import annotations

from typing import Final

LOCKFILE: Final = "lockfile"
VENDORED: Final = "vendored"
GENERATED: Final = "generated"
BINARY: Final = "binary"

# Compared case-insensitively: macOS filesystems are, and so are some ecosystems' tools.
_LOCKFILE_NAMES = frozenset(
    name.lower()
    for name in (
        "pnpm-lock.yaml",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "deno.lock",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pdm.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "go.sum",
        "go.work.sum",
        "composer.lock",
        "mix.lock",
        "pubspec.lock",
        "Podfile.lock",
        "Package.resolved",
        "packages.lock.json",
        "gradle.lockfile",
        "flake.lock",
    )
)
_VENDORED_DIRECTORIES = frozenset(
    {"vendor", "node_modules", "third_party", "third-party", "bower_components"}
)
_GENERATED_DIRECTORIES = frozenset({"dist", "build", "__generated__"})
_GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".js.map",
    ".css.map",
    "_pb2.py",
    "_pb2.pyi",
    "_pb2_grpc.py",
    ".pb.go",
    ".pb.cc",
    ".pb.h",
    ".g.dart",
    ".freezed.dart",
)
_GENERATED_INFIX = ".generated."


def directory_skip_reason(directory: str) -> str | None:
    """Why everything under ``directory`` (a repo-relative path) is skipped, if it is.

    Examples:
        >>> directory_skip_reason("web/node_modules")
        'vendored'
        >>> directory_skip_reason("src") is None
        True
    """
    parts = {part.lower() for part in directory.split("/")}
    if parts & _VENDORED_DIRECTORIES:
        return VENDORED
    if parts & _GENERATED_DIRECTORIES:
        return GENERATED
    return None


def auto_skip_reason(path: str, is_binary: bool) -> str | None:
    """The reason a changed file is skipped automatically, or ``None`` to review it.

    Args:
        path: repo-relative, ``/``-separated (``web/pnpm-lock.yaml``).
        is_binary: whether the content looks binary (a NUL byte near the start, as git
            decides).

    Returns:
        ``"lockfile"``, ``"vendored"``, ``"generated"`` or ``"binary"``, checked in that
        order, so a vendored lockfile is a lockfile and a vendored image is vendored.
    """
    directory, _, name = path.rpartition("/")
    lowered = name.lower()
    if lowered in _LOCKFILE_NAMES:
        return LOCKFILE
    reason = directory_skip_reason(directory)
    if reason is not None:
        return reason
    if lowered.endswith(_GENERATED_SUFFIXES) or _GENERATED_INFIX in lowered:
        return GENERATED
    return BINARY if is_binary else None
