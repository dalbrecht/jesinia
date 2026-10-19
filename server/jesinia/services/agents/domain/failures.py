# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a failed agent turn's message says about the failure, whichever CLI reported it."""

from __future__ import annotations

import re

# A provider that is momentarily out of capacity, rate limiting, or failing on its side
# (5xx): asking again a little later usually works. Matched against the runner's own error
# text, since neither CLI gives a machine-readable code for these.
_TRANSIENT = re.compile(
    r"at capacity|rate[ _-]?limit|too many requests|overloaded|temporarily unavailable"
    r"|service unavailable|bad gateway|gateway time-?out|internal server error"
    r"|\b(?:429|500|502|503|504|529)\b",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_KEY = re.compile(r"\b(?:sk|ghp|gho|ghs|github_pat|xox[abp])[-_][A-Za-z0-9_-]{8,}")


def is_transient_failure(message: str) -> bool:
    """Whether ``message`` (a runner's error) reports a passing provider problem worth a
    retry after a pause, rather than a failure that would only repeat."""
    return _TRANSIENT.search(message) is not None


def redact_secrets(text: str) -> str:
    """``text`` with bearer tokens and API keys masked, for logs and records: a CLI's
    stderr can echo a request header or a key."""
    return _API_KEY.sub("***", _BEARER.sub(r"\1***", text))
