# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strip credentials from git output before it is stored, shown, or published."""

from __future__ import annotations

import re

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")
MAX_LEN = 300


def redact(text: str) -> str:
    """Mask ``user:secret@`` in URLs and cap the length (status is shown in the UI)."""
    clean = _URL_USERINFO.sub(r"\g<scheme>***@", text.strip())
    return clean if len(clean) <= MAX_LEN else clean[: MAX_LEN - 1] + "…"
