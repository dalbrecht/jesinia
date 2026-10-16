# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which resolved addresses outbound fetches may reach: public unicast only."""

from __future__ import annotations

import ipaddress

# IPv6 ranges that carry (or translate to) an IPv4 address: never reached directly.
_EMBEDDING = tuple(
    ipaddress.ip_network(n)
    for n in ("::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not ip.is_multicast


def is_public(address: str) -> bool:
    """False for loopback, private, link-local, shared, reserved and multicast ranges, and
    for IPv6 forms that embed an IPv4 address (mapped, compatible, NAT64, 6to4, Teredo)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _public(ip.ipv4_mapped)
        if any(ip in net for net in _EMBEDDING):
            return False
    return _public(ip)
