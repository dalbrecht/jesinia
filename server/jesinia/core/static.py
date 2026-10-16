# SPDX-License-Identifier: AGPL-3.0-or-later
"""Serve the built PWAs from ``jesinia/static`` (spec §3.5).

``/`` and ``/settings/`` are the launcher; ``/<app>/…`` is ``static/<app>/``. A path
that is not a file falls back to the app's ``index.html`` (client-side routing). Assets
are public: they hold no data, and every API call they make is authenticated.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

STATIC = Path(__file__).resolve().parent.parent / "static"
LAUNCHER = "launcher"
LAUNCHER_ROUTES = frozenset({"", "settings"})
RESERVED = frozenset({"api", "mcp", "login", "healthz", "docs", "redoc", "openapi.json"})


def _file(base: Path, rel: str) -> Path | None:
    candidate = (base / rel).resolve()
    if not candidate.is_relative_to(base.resolve()) or not candidate.is_file():
        return None
    return candidate


def static_router(root: Path = STATIC) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/{path:path}")
    async def serve(path: str) -> FileResponse:
        first, _, rest = path.partition("/")
        if first in RESERVED:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        app_dir = (root / first).resolve()
        # A non-normalized ".." first segment must not pick a base outside the root.
        is_app = app_dir.parent == root.resolve() and app_dir.is_dir()
        if first and first not in LAUNCHER_ROUTES and is_app:
            base, rel = app_dir, rest
        else:
            base, rel = root / LAUNCHER, path if first not in LAUNCHER_ROUTES else rest
        found = _file(base, rel) if rel else None
        if found is None:
            if "." in rel.rsplit("/", 1)[-1]:  # a missing asset, not a client route
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            found = _file(base, "index.html")
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "this app is not built (make build)")
        if "assets" in found.relative_to(base.resolve()).parts[:-1]:
            cache = "public, max-age=31536000, immutable"  # content-hashed by Vite
        else:
            cache = "no-cache"
        media = "application/manifest+json" if found.suffix == ".webmanifest" else None
        return FileResponse(found, media_type=media, headers={"Cache-Control": cache})

    return router
