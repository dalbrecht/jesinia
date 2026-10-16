# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI provenance comments (spec §5.3)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_LABEL = re.compile(r"[^\w.:/@+-]")


def inert(text: str) -> str:
    """Neutralise HTML comment markers so text cannot open or close a provenance block."""
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def provenance_label(value: str) -> str:
    """An agent or model name safe inside the marker: one token, no comment syntax."""
    return _LABEL.sub("-", value)[:80] or "unknown"


def wrap_ai(text: str, *, agent: str, model: str, at: datetime, session: str) -> str:
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"<!-- jesinia:ai agent={agent} model={model} at={stamp} session={session} -->\n"
        f"{text}\n<!-- /jesinia:ai -->"
    )
