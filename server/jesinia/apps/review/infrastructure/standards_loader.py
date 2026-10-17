# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loads the layered review standards for one PR (spec §5.2).

The repo's own files (``.standards.yml`` and ``.jesinia/review.md``) are read from the PR's
**base** commit in the bare mirror, through the workspace's hardened git: never from the
PR's head (a PR can't rewrite the standards it is judged by) and never from a checkout (a
symlink in the repo is never followed). The author's coding-standards checkout and their
own standards folder are local files, opened without following symlinks and only when
they are regular files. Every document is capped at ``MAX_DOCUMENT_BYTES``.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from jesinia.apps.review.application.ports import MirrorFiles
from jesinia.apps.review.domain.standards import (
    MAX_DOCUMENT_BYTES,
    REPO_REVIEW_FILE,
    REPO_STANDARDS_CONFIG,
    Document,
    InvalidStandardsConfigError,
    Layer,
    Override,
    Standards,
    StandardsDocumentTooLargeError,
    StandardsUnreadableError,
    applies,
    expand_languages,
    layer_standards,
    parse_languages,
)
from jesinia.apps.review.domain.workspace import (
    MirrorFileTooLargeError,
    WorkspaceError,
    canonical_repo,
)

log = logging.getLogger(__name__)

# The coding-standards checkout's layout (c65llc/coding-standards): the documents a review
# applies live in these folders of its ``standards/`` directory. ``agents/`` holds agent
# configs and ``shared/`` the setup's building blocks, apart from the core standards.
BASE_FOLDERS = ("architecture", "languages", "process", "security")
BASE_FILES = ("shared/core-standards.md",)
_SKIPPED_NAMES = frozenset({"readme.md"})


class StandardsLoader:
    """``StandardsSource`` over the workspace's mirrors and two local folders.

    Args:
        mirror: reads committed files from a repo's bare mirror.
        standards_dir: the author's coding-standards checkout (Settings → Review), or
            ``None`` when it isn't set; asked on every load, so a change applies at once.
    """

    def __init__(self, mirror: MirrorFiles, standards_dir: Callable[[], Path | None]) -> None:
        self._mirror = mirror
        self._standards_dir = standards_dir

    async def load(
        self, repo: str, base_sha: str, user_dir: Path | None, overrides: Sequence[Override]
    ) -> Standards:
        """The merged standards for a PR of ``repo`` whose base commit is ``base_sha``.

        A missing file or folder means that layer is absent.

        Raises:
            StandardsDocumentTooLargeError: a document over ``MAX_DOCUMENT_BYTES``.
            InvalidStandardsConfigError: the repo's ``.standards.yml`` can't be parsed.
            StandardsUnreadableError: the mirror couldn't be read (e.g. it lacks the commit).
        """
        try:
            canonical = canonical_repo(repo)
        except WorkspaceError as exc:
            raise StandardsUnreadableError(str(exc)) from exc
        config = await self._repo_file(canonical, base_sha, REPO_STANDARDS_CONFIG)
        languages = expand_languages(_languages(config))
        review = await self._repo_file(canonical, base_sha, REPO_REVIEW_FILE)
        base = await asyncio.to_thread(_base_documents, self._standards_dir(), languages)
        user = await asyncio.to_thread(_user_documents, user_dir)
        repo_layer = [] if review is None else [Document(REPO_REVIEW_FILE, "repo", _text(review))]
        return layer_standards([*base, *repo_layer, *user], overrides)

    async def _repo_file(self, repo: str, sha: str, path: str) -> bytes | None:
        try:
            return await self._mirror.show_file(repo, sha, path, limit=MAX_DOCUMENT_BYTES)
        except MirrorFileTooLargeError as exc:
            raise StandardsDocumentTooLargeError(
                f"{path} in {repo} is over {MAX_DOCUMENT_BYTES // 1024} KB: {exc}"
            ) from exc
        except WorkspaceError as exc:
            raise StandardsUnreadableError(
                f"could not read {path} at {sha} in {repo}: {exc}"
            ) from exc


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _languages(config: bytes | None) -> tuple[str, ...]:
    if config is None:
        return ()
    try:
        return parse_languages(yaml.safe_load(config.decode("utf-8")))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InvalidStandardsConfigError(f"{REPO_STANDARDS_CONFIG} is not valid: {exc}") from exc


def _read_regular(path: Path) -> str | None:
    """``path``'s text if it is a regular file, opened without following a symlink and
    without blocking on a FIFO; ``None`` for anything else or nothing there.

    Raises:
        StandardsDocumentTooLargeError: over ``MAX_DOCUMENT_BYTES``.
        StandardsUnreadableError: it exists but can't be read (permissions).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            if exc.errno == errno.ELOOP:
                log.warning("review.standards skipped %s: a symlink is never followed", path)
            return None
        raise StandardsUnreadableError(f"could not open {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            log.warning("review.standards skipped %s: not a regular file", path)
            return None
        data = b""
        while chunk := os.read(fd, MAX_DOCUMENT_BYTES + 1 - len(data)):
            data += chunk
            if len(data) > MAX_DOCUMENT_BYTES:
                raise StandardsDocumentTooLargeError(
                    f"{path.name} is over {MAX_DOCUMENT_BYTES // 1024} KB"
                )
        return _text(data)
    finally:
        os.close(fd)


def _folder_documents(folder: Path, layer: Layer, keep: Callable[[str], bool]) -> list[Document]:
    """The Markdown documents directly in ``folder`` that ``keep`` accepts, by name; a
    folder that is missing, or is a symlink, contributes nothing."""
    if folder.is_symlink() or not folder.is_dir():
        return []
    documents: list[Document] = []
    for entry in sorted(folder.iterdir()):
        name = entry.name
        if not name.endswith(".md") or name.lower() in _SKIPPED_NAMES or not keep(name):
            continue
        text = _read_regular(entry)
        if text is not None:
            documents.append(Document(name, layer, text))
    return documents


def _base_documents(standards_dir: Path | None, languages: frozenset[str]) -> list[Document]:
    if standards_dir is None or not standards_dir.is_dir():
        return []
    root = standards_dir / "standards"
    root = root if root.is_dir() else standards_dir
    documents: list[Document] = []
    for folder in BASE_FOLDERS:
        documents += _folder_documents(root / folder, "base", lambda name: applies(name, languages))
    for relative in BASE_FILES:
        text = _read_regular(root / relative)
        if text is not None:
            documents.append(Document(Path(relative).name, "base", text))
    return documents


def _user_documents(user_dir: Path | None) -> list[Document]:
    if user_dir is None:
        return []
    return _folder_documents(user_dir, "user", lambda name: True)
