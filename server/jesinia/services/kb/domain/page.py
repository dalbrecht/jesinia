# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pages and their identity (spec §5.1)."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import NewType

PageId = NewType("PageId", str)


def new_page_id() -> PageId:
    """A lowercase, dashed UUIDv7 (time-ordered)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits; 74 are used
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a: 12 bits
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand & ((1 << 62) - 1)  # rand_b: 62 bits
    return PageId(str(uuid.UUID(int=value)))


@dataclass(frozen=True)
class Page:
    id: PageId
    title: str
    path: PurePosixPath  # relative to the KB root, NFC
    body: str
    jesinia: Mapping[str, object] = field(default_factory=dict)
    foreign: Mapping[str, object] = field(default_factory=dict)


def source_of(jesinia: Mapping[str, object]) -> str | None:
    """The page's source URL: ``jesinia.source`` when it is a non-empty string."""
    value = jesinia.get("source")
    return value if isinstance(value, str) and value else None
