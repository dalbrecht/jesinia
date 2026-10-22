# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent-only pages and folders (ADR 0011).

A page whose front matter has ``jesinia: {access: agents}`` is agent-only: agents read and
write it through MCP, but the user-facing routes (the Notes app) neither list nor open it.
When that page is also ``kind: folder``, the lock covers its whole folder and every
subfolder. The marker lives in the page, so it travels with the KB through git sync.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal

AGENTS_ONLY: Final = "agents"
FOLDER_KIND: Final = "folder"

LockScope = Literal["page", "folder"]


def lock_scope(jesinia: Mapping[str, object]) -> LockScope | None:
    """``folder`` for a locked folder page, ``page`` for any other locked page, else None.

    Only the exact value ``agents`` locks, so a typo never half-hides a page.
    """
    if jesinia.get("access") != AGENTS_ONLY:
        return None
    return "folder" if jesinia.get("kind") == FOLDER_KIND else "page"


def _key(path: PurePosixPath) -> tuple[str, ...]:
    # macOS file systems ignore case: `91 memory/x.md` is the same file as `91 Memory/x.md`.
    return tuple(part.casefold() for part in path.parts)


@dataclass(frozen=True)
class AgentOnly:
    """Which KB paths are agent-only, from the locked pages' paths and scopes."""

    pages: frozenset[tuple[str, ...]] = frozenset()
    folders: frozenset[tuple[str, ...]] = frozenset()

    @classmethod
    def of(cls, locks: Iterable[tuple[PurePosixPath, LockScope]]) -> AgentOnly:
        pages: set[tuple[str, ...]] = set()
        folders: set[tuple[str, ...]] = set()
        for path, scope in locks:
            pages.add(_key(path))
            folder = _key(path.parent)
            # One page must never lock the whole KB: a root-level folder page locks itself.
            if scope == "folder" and folder:
                folders.add(folder)
        return cls(frozenset(pages), frozenset(folders))

    def __bool__(self) -> bool:
        return bool(self.pages or self.folders)

    def covers_folder(self, folder: PurePosixPath) -> bool:
        """Whether a page written in ``folder`` would be agent-only."""
        key = _key(folder)
        return any(key[:n] in self.folders for n in range(1, len(key) + 1))

    def covers(self, path: PurePosixPath) -> bool:
        """Whether the page at ``path`` is agent-only."""
        return _key(path) in self.pages or self.covers_folder(path.parent)
