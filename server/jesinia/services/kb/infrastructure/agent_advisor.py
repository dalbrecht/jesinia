# SPDX-License-Identifier: AGPL-3.0-or-later
"""``AgentTagAdvisor``: the tag advisor backed by a one-shot ask to the user's agent.

The text to tag may be a web article, so it reaches the agent fenced as untrusted data;
the catalog (the user's own tag list) is the role prompt. Suggestions are a secondary,
best-effort feature: an unavailable runner, a timeout, an error or an unparsable reply
all mean "no suggestions", logged with the cause.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from jesinia.services.agents.application.packs import ContextPack, UntrustedDoc
from jesinia.services.kb.domain.tag_catalog import CatalogEntry
from jesinia.services.kb.domain.tags import is_valid_tag, normalise_tag

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 5
MAX_TEXT = 20_000  # characters of the text sent to the agent
ASK_TIMEOUT = 45.0

ROLE = (
    "You help keep the user's knowledge base tags consistent. A tag is lowercase words "
    "joined by hyphens, written here with a leading #. The user's tag catalog:\n\n"
)
SUGGEST_PROMPT = (
    "TAGS: Suggest up to five tags for the document below. Prefer catalog tags when one "
    "fits; propose a new tag only when none does. Reply with only a JSON array of tag "
    'strings, for example ["sqlite", "rust"].'
)
SAME_MEANING_PROMPT = (
    "TAGS: Does a catalog tag mean the same as #{tag}? Reply with only a JSON array "
    "holding that one catalog tag, or [] if none does."
)


@runtime_checkable
class OneShotAsker(Protocol):
    """What the advisor needs from the agents service (``SessionService``)."""

    async def one_shot(
        self, runner: str, pack: ContextPack, prompt: str, *, timeout: float = ASK_TIMEOUT
    ) -> str: ...


def _render_catalog(catalog: list[CatalogEntry]) -> str:
    lines = []
    for entry in catalog:
        line = f"- #{entry.tag}"
        if entry.meaning:
            line += f": {entry.meaning}"
        if entry.aliases:
            line += f" (aliases: {', '.join(entry.aliases)})"
        lines.append(line)
    return "\n".join(lines) if lines else "(empty: no tags yet)"


def _json_array(reply: str) -> object:
    try:
        return json.loads(reply)
    except json.JSONDecodeError:
        pass
    # Agents often wrap the array in prose or a code fence: try the outermost brackets.
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(reply[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_tags(reply: str) -> list[str]:
    """The tags in ``reply``'s JSON array, normalised, valid and de-duplicated; ``[]`` if
    the reply holds no such array."""
    data = _json_array(reply)
    if not isinstance(data, list):
        return []
    found: dict[str, None] = {}
    for item in data:
        if isinstance(item, str) and is_valid_tag(tag := normalise_tag(item)):
            found.setdefault(tag, None)
    return list(found)


class AgentTagAdvisor:
    """Implements ``TagAdvisor``. ``sessions`` and ``runner`` are looked up at call time,
    since the kb module starts before the agents module."""

    def __init__(
        self,
        sessions: Callable[[], OneShotAsker | None],
        runner: Callable[[], str | None],
        *,
        timeout: float = ASK_TIMEOUT,
    ) -> None:
        self._sessions = sessions
        self._runner = runner
        self._timeout = timeout

    async def _ask(self, pack: ContextPack, prompt: str) -> list[str]:
        sessions, runner = self._sessions(), self._runner()
        if sessions is None or runner is None:
            return []
        try:
            reply = await sessions.one_shot(runner, pack, prompt, timeout=self._timeout)
        # Secondary feature: whatever the agent run raises, tag operations carry on.
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("tag advisor ask via %s failed (%s: %s)", runner, type(exc).__name__, exc)
            return []
        return parse_tags(reply)

    async def suggest(self, text: str, catalog: list[CatalogEntry]) -> list[str]:
        pack = ContextPack(
            ROLE + _render_catalog(catalog),
            untrusted={"text.md": UntrustedDoc("text-to-tag", text[:MAX_TEXT])},
        )
        return (await self._ask(pack, SUGGEST_PROMPT))[:MAX_SUGGESTIONS]

    async def same_meaning(self, tag: str, catalog: list[CatalogEntry]) -> str | None:
        names = {name: entry.tag for entry in catalog for name in (entry.tag, *entry.aliases)}
        prompt = SAME_MEANING_PROMPT.format(tag=normalise_tag(tag))
        for answer in await self._ask(ContextPack(ROLE + _render_catalog(catalog)), prompt):
            if answer in names:
                return names[answer]
        return None
