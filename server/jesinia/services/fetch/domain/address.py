# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which resolved addresses outbound fetches may reach: public unicast only."""

from __future__ import annotations

import ipaddress


def is_public(address: str) -> bool:
    """False for loopback, private, link-local, shared, reserved and multicast ranges."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast
