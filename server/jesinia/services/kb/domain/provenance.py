# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI provenance comments (spec §5.3)."""

from __future__ import annotations

from datetime import UTC, datetime


def wrap_ai(text: str, *, agent: str, model: str, at: datetime, session: str) -> str:
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"<!-- jesinia:ai agent={agent} model={model} at={stamp} session={session} -->\n"
        f"{text}\n<!-- /jesinia:ai -->"
    )
