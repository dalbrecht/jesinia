# SPDX-License-Identifier: AGPL-3.0-or-later
"""Talking to the running server from the CLI (loopback, install token)."""

from __future__ import annotations

from typing import Any

import httpx

from jesinia.core.config import Config


class ServerDown(RuntimeError):  # noqa: N818 - reads as a state, like the plan's names
    """Nothing answers on ``127.0.0.1:<port>``."""


def call(
    cfg: Config, method: str, path: str, *, token: str | None = None, timeout: float = 30.0
) -> Any:
    """JSON from ``method path`` on the local server; ``ServerDown`` if it is not running."""
    headers = {"Authorization": f"Bearer {token or cfg.token}"}
    try:
        resp = httpx.request(
            method,
            f"http://127.0.0.1:{cfg.port}{path}",
            headers=headers,
            timeout=timeout,
            trust_env=False,
        )
    except httpx.TransportError as exc:
        raise ServerDown(f"the Jesinia server is not running on port {cfg.port}") from exc
    resp.raise_for_status()
    return resp.json()
