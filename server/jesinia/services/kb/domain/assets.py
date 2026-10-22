# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page assets (``_assets/<page-id>/<file>``, kb-format): which names and bytes are served."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from jesinia.services.kb.domain.tags import mask_code

# A page id as Jesinia writes it: a lowercase, dashed UUID. Also keeps it one path segment.
_PAGE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# One plain file name: no folders, no leading dot, no spaces or shell-special characters.
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9]+)?")
# A relative link into the root asset folder, as a Markdown link or an HTML ``src``/``href``
# value: only these are links (an ``_assets`` path in prose or code is left alone).
_ASSET_LINK = re.compile(
    r"(?P<lead>\]\(<?|\b(?:src|href)=[\"']?)(?P<up>(?:\.\./)*)(?P<rest>_assets/"
    + _PAGE_ID.pattern
    + r"/)"
)


def is_page_id(text: str) -> bool:
    """Whether ``text`` is a page id (and so a safe asset folder name)."""
    return _PAGE_ID.fullmatch(text) is not None


def is_asset_file_name(text: str) -> bool:
    """Whether ``text`` is one plain asset file name (never ``..``, a dotfile or a path)."""
    return ASSET_NAME.fullmatch(text) is not None


def image_media_type(data: bytes) -> str | None:
    """The media type of the raster image ``data`` starts with, whatever its name says; None
    for anything else. SVG is refused too: it can carry script."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def rebase_asset_links(body: str, from_folder: PurePosixPath, to_folder: PurePosixPath) -> str:
    """``body`` with each relative link to the KB-root ``_assets/`` folder rewritten for a
    page moved from ``from_folder`` to ``to_folder``, so its images survive the move.

    A link inside code is an example, not a link, and one that does not reach the root asset
    folder from ``from_folder`` (it climbs too few or too many levels) is not one Jesinia
    wrote: both are left as they are.
    """
    old_up = "../" * len(from_folder.parts)
    new_up = "../" * len(to_folder.parts)

    pieces: list[str] = []
    cursor = 0
    # Matched on a copy with code blanked out, so examples inside code stay as written.
    for match in _ASSET_LINK.finditer(mask_code(body)):
        if match["up"] == old_up:
            pieces.append(body[cursor : match.start("up")])
            pieces.append(new_up)
            cursor = match.end("up")
    pieces.append(body[cursor:])
    return "".join(pieces)
