# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public surface of the fetch service for apps that render fetched or feed-supplied HTML."""

from __future__ import annotations

from jesinia.services.fetch.infrastructure.sanitize import sanitize_html

__all__ = ["sanitize_html"]
