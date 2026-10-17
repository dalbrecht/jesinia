# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared reading of GitHub payloads for the typed clients (``PrClient``, ``IssueClient``).

Every read goes through ``GitHubService.get`` (ETag-revalidated). The shape helpers turn an
unexpected payload into ``GitHubPayloadError`` instead of a ``KeyError`` deep in a caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from jesinia.services.github.application.service import (
    GitHubError,
    GitHubRequestError,
    GitHubService,
    github_error_message,
)
from jesinia.services.github.domain.pr import is_bot_account

PER_PAGE = 100
# A deleted account shows up as an empty author; GitHub itself renders it as "ghost".
GHOST = "ghost"


class GitHubPayloadError(GitHubError):
    """GitHub answered with a shape this client does not understand."""


def mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubPayloadError(f"expected an object for {what}, got {type(value).__name__}")
    return value


def list_of(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise GitHubPayloadError(f"expected a list for {what}, got {type(value).__name__}")
    return value


def text(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise GitHubPayloadError(f"expected text for {key!r}, got {type(value).__name__}")
    return value


def optional_text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def number(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GitHubPayloadError(f"expected a number for {key!r}, got {type(value).__name__}")
    return value


def optional_number(data: Mapping[str, object], key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def flag(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise GitHubPayloadError(f"expected true/false for {key!r}, got {type(value).__name__}")
    return value


def optional_mapping(data: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    value = data.get(key)
    return value if isinstance(value, Mapping) else None


def optional_time(data: Mapping[str, object], key: str) -> datetime | None:
    value = optional_text(data, key)
    return datetime.fromisoformat(value) if value else None


def account(data: Mapping[str, object], key: str) -> tuple[str, bool]:
    """(login, is_bot) of a ``user``-shaped field; a deleted account is ``ghost``."""
    found = optional_mapping(data, key)
    if found is None:
        return GHOST, False
    login = text(found, "login")
    return login, is_bot_account(login, optional_text(found, "type"))


async def get_json(
    github: GitHubService, path: str, params: Mapping[str, str] | None = None
) -> object:
    """The body of a successful GET; any other answer raises with GitHub's message."""
    response = await github.get(path, params)
    if not 200 <= response.status < 300:
        raise GitHubRequestError(response.status, github_error_message(response.data))
    return response.data


async def get_page(
    github: GitHubService,
    path: str,
    page: int,
    params: Mapping[str, str] | None = None,
    items_key: str | None = None,
) -> tuple[list[Mapping[str, object]], bool]:
    """One page of a list and whether another may follow (this one was full)."""
    query = {**(params or {}), "per_page": str(PER_PAGE), "page": str(page)}
    data = await get_json(github, path, query)
    if items_key is not None:
        data = mapping(data, path).get(items_key)
    batch = list_of(data, path)
    return [mapping(item, path) for item in batch], len(batch) == PER_PAGE


async def get_pages(
    github: GitHubService,
    path: str,
    params: Mapping[str, str] | None = None,
    items_key: str | None = None,
    max_pages: int | None = None,
) -> list[Mapping[str, object]]:
    """Follow ``page`` until a short page, or ``max_pages``; GitHub caps these lists
    (3000 files, 250 commits, 1000 search results), so the loop always ends."""
    items: list[Mapping[str, object]] = []
    page = 1
    while True:
        batch, has_more = await get_page(github, path, page, params, items_key)
        items.extend(batch)
        if not has_more or page == max_pages:
            return items
        page += 1
