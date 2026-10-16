# SPDX-License-Identifier: AGPL-3.0-or-later
"""Talking to the running server from the CLI (loopback, install token)."""

from __future__ import annotations

from typing import Any

import httpx

from jesinia.core.config import Config


class ServerBusy(RuntimeError):  # noqa: N818 - reads as a state, like ServerDown
    """The server is there but didn't answer in time; never treat it as stopped."""


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
    except httpx.ConnectError as exc:  # nothing listening: the server is down
        raise ServerDown(f"the Jesinia server is not running on port {cfg.port}") from exc
    except httpx.TransportError as exc:  # it answered too slowly or dropped: it may be busy
        raise ServerBusy(
            f"the Jesinia server on port {cfg.port} did not answer ({type(exc).__name__}); "
            "it may still be working - check `jesinia status`"
        ) from exc
    resp.raise_for_status()
    return resp.json()
