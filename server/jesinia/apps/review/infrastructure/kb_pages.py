# SPDX-License-Identifier: AGPL-3.0-or-later
"""``KnowledgePages`` over the KB service: the knowledge model reads KB pages that declare
the code they are about (``jesinia.code``, spec §6.1). Page content is untrusted data: it is
parsed by the KB service and only read, never run."""

from __future__ import annotations

from datetime import UTC, datetime

from jesinia.apps.review.application.errors import KnowledgeSourceError
from jesinia.apps.review.application.knowledge import PageContent, PageRev
from jesinia.services.kb.application.errors import KbError, PageNotFound
from jesinia.services.kb.application.service import KbService
from jesinia.services.kb.domain.page import PageId


class KbKnowledgePages:
    """Pages and their revisions from the KB index; content through ``KbService.read``."""

    def __init__(self, kb: KbService) -> None:
        self._kb = kb

    def page_revs(self) -> list[PageRev]:
        return [
            PageRev(str(row.id), row.rev, datetime.fromtimestamp(row.mtime, UTC))
            for row in self._kb.index.all()
        ]

    async def page(self, page_id: str) -> PageContent | None:
        """The page's front matter and body; ``None`` when it is gone since it was listed.

        Raises:
            KnowledgeSourceError: the KB couldn't be read (no KB folder, say), with its cause.
        """
        try:
            found = await self._kb.read(PageId(page_id))
        except PageNotFound:
            return None
        except KbError as exc:
            raise KnowledgeSourceError(f"KB page {page_id} couldn't be read: {exc}") from exc
        return PageContent(found.page.jesinia, found.page.body)
