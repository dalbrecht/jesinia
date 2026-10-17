# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review app's one error root: every typed error the app raises derives from it, so a
caller (the REST layer, a job) can tell the app's own failures from anything unexpected."""

from __future__ import annotations


class ReviewError(Exception):
    """Base for the review app's typed errors; the message is Jesinia's own words."""
