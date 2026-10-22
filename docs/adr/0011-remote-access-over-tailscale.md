# ADR 0011: Remote access over Tailscale

- **Status:** Accepted
- **Date:** 2026-10-22
- **Related:** foundation spec §7.3; [remote access spec](../specs/2026-10-22-remote-access-design.md);
  [ADR 0010](0010-persistent-browser-sessions.md); [security conventions](../conventions/security.md)

## Context

Spec §7.3 binds `127.0.0.1` only and rejects any non-local `Host`. The owner now wants
Jesinia on an iPhone and iPad (the PWAs, and NetNewsWire syncing against it), over their
Tailscale network, and later from a dedicated machine.

## Decision

- **Keep the loopback bind; let `tailscale serve` front it.** Tailscale terminates HTTPS on
  the tailnet name with its own certificate and proxies to `127.0.0.1:<port>`. Jesinia
  opens no new socket, so only tailnet devices can reach it, and only over TLS.
- **Allow exactly one extra host.** `server.remote_host` names the tailnet host. The guard
  accepts it (and its `:443` form) and its `https://` origin; everything else still gets
  421. DNS-rebinding defense is unchanged, because the allowed name is the owner's own.
- **Remote responses add HSTS; remote cookies add `Secure`.**
- **Same auth everywhere.** Remote browsers sign in with the existing single-use nonce,
  via a link minted on the Mac (`jesinia login --remote`). Bearer clients use the same
  tokens as locally.

## Rejected

- **Bind `0.0.0.0` or the Tailscale interface.** Puts plain HTTP on a network interface and
  makes Jesinia handle TLS.
- **Trust Tailscale identity headers.** Any local process can send them to the loopback
  port.
- **Tailscale Funnel or a public hostname.** Out of scope; the tailnet is the boundary.

## Consequences

The install token and session cookies now grant access from any device on the tailnet,
not only from this Mac. A tailnet device that is compromised still needs a credential.
Turning remote access off (`jesinia remote disable`) removes the proxy and the allowed
host.
