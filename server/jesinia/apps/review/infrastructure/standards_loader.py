# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loads the layered review standards for one PR (spec §5.2).

The repo's own files (``.standards.yml`` and ``.jesinia/review.md``) are read at the commit
the caller names, from the bare mirror, through the workspace's hardened git. Jobs name the
tip of the repo's **default branch** (R39): never the PR's head, and never its base, which
for a stacked PR is another PR's branch, so no PR can rewrite the standards it is judged
by. Nothing is read from a checkout, so a symlink in the repo is never followed. Parsing
and layering run in a worker thread, so a large document never stalls the event loop.

The author's coding-standards checkout and their own standards folder are local files,
opened without following symlinks and only when they are regular files. Every document is
capped at ``MAX_DOCUMENT_BYTES`` (the config at ``MAX_CONFIG_BYTES``).
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
    MAX_CONFIG_BYTES,
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
    check_config_nesting,
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
        self, repo: str, standards_sha: str, user_dir: Path | None, overrides: Sequence[Override]
    ) -> Standards:
        """The merged standards for a PR of ``repo``, with the repo layer read at commit
        ``standards_sha`` (the default branch's tip).

        A missing file or folder means that layer is absent; so does a ``review.md`` that
        isn't text (not UTF-8, or holding a NUL).

        Raises:
            StandardsDocumentTooLargeError: a document over its cap.
            TooManyStandardsSectionsError: a document with too many rules.
            InvalidStandardsConfigError: the repo's ``.standards.yml`` can't be parsed.
            StandardsUnreadableError: the mirror couldn't be read (e.g. it lacks the commit).
        """
        try:
            canonical = canonical_repo(repo)
        except WorkspaceError as exc:
            raise StandardsUnreadableError(str(exc)) from exc
        config = await self._repo_file(
            canonical, standards_sha, REPO_STANDARDS_CONFIG, MAX_CONFIG_BYTES
        )
        review = await self._repo_file(
            canonical, standards_sha, REPO_REVIEW_FILE, MAX_DOCUMENT_BYTES
        )
        return await asyncio.to_thread(
            _assemble, config, review, self._standards_dir(), user_dir, list(overrides)
        )

    async def _repo_file(self, repo: str, sha: str, path: str, limit: int) -> bytes | None:
        try:
            return await self._mirror.show_file(repo, sha, path, limit=limit)
        except MirrorFileTooLargeError as exc:
            raise StandardsDocumentTooLargeError(
                f"{path} in {repo} is over {limit // 1024} KB: {exc}"
            ) from exc
        except WorkspaceError as exc:
            raise StandardsUnreadableError(
                f"could not read {path} at {sha} in {repo}: {exc}"
            ) from exc


def _assemble(
    config: bytes | None,
    review: bytes | None,
    standards_dir: Path | None,
    user_dir: Path | None,
    overrides: list[Override],
) -> Standards:
    """Parse and layer everything (CPU-bound: run in a worker thread)."""
    languages = expand_languages(_languages(config))
    base = _base_documents(standards_dir, languages)
    repo_text = _repo_review_text(review)
    repo_layer = [] if repo_text is None else [Document(REPO_REVIEW_FILE, "repo", repo_text)]
    return layer_standards([*base, *repo_layer, *_user_documents(user_dir)], overrides)


def _repo_review_text(review: bytes | None) -> str | None:
    if review is None:
        return None
    try:
        text = review.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or "\0" in text:
        log.warning("review.standards ignored %s: it is not text", REPO_REVIEW_FILE)
        return None
    return text


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _languages(config: bytes | None) -> tuple[str, ...]:
    if config is None:
        return ()
    try:
        text = config.decode("utf-8")
        check_config_nesting(text)
        return parse_languages(yaml.safe_load(text))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError, ValueError) as exc:
        # ValueError and RecursionError: what a parser raises on hostile input it didn't
        # anticipate (the nesting check above should stop the deep cases first).
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
