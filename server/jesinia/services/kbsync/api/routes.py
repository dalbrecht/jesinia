# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/api/kbsync/…`` routes and the ``kb_sync_status`` / ``kb_sync_now`` MCP tools."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Protocol

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from jesinia.core.module import CallContext, ToolDef
from jesinia.services.kbsync.application.service import SyncService


class SyncHost(Protocol):
    @property
    def service(self) -> SyncService: ...


class EmptyIn(BaseModel):
    pass


def build_router(host: SyncHost) -> APIRouter:
    r = APIRouter(prefix="/api/kbsync", tags=["kbsync"])

    @r.get("/status")
    async def get_status() -> dict[str, object]:
        return host.service.status().to_dict()

    @r.post("/run")
    async def run_now() -> dict[str, object]:
        result = await host.service.run_cycle("manual")
        return {"result": asdict(result), "status": host.service.status().to_dict()}

    @r.post("/resume")
    async def resume() -> dict[str, object]:
        await host.service.resume()
        return host.service.status().to_dict()

    @r.post("/init")
    async def init_repo() -> dict[str, object]:
        try:
            await host.service.init_repo()
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return host.service.status().to_dict()

    return r


def build_tools(host: SyncHost) -> Sequence[ToolDef]:
    async def kb_sync_status(args: BaseModel, call: CallContext) -> object:
        return host.service.status().to_dict()

    async def kb_sync_now(args: BaseModel, call: CallContext) -> object:
        return asdict(await host.service.run_cycle("manual"))

    return [
        ToolDef(
            "kb_sync_status",
            "KB git sync status: mode, problem, last run and result, ahead/behind.",
            EmptyIn,
            kb_sync_status,
            False,
        ),
        ToolDef(
            "kb_sync_now",
            "Commit, pull, and push the KB now (never forces; pauses on conflict).",
            EmptyIn,
            kb_sync_now,
            True,
        ),
    ]
