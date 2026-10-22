# Remote access over Tailscale

- **Status:** Accepted
- **Date:** 2026-10-22
- **Related:** foundation spec §7.3, §7.4; [ADR 0011](../adr/0011-remote-access-over-tailscale.md);
  [security conventions](../conventions/security.md)

## 1. Intent

The owner wants to read and work in Jesinia from an iPhone and iPad, both as the PWAs and
as the server behind NetNewsWire (a follow-up spec). Today Jesinia answers only on
`127.0.0.1` and refuses any other `Host`, so nothing off the Mac can reach it. Tailscale
is the network for now; Jesinia will later move to a dedicated computer, which must only
change the hostname.

## 2. Goals

- All of Jesinia (PWAs, HTTP APIs, MCP) is reachable from the owner's tailnet over HTTPS.
- A browser on another device can sign in without the install token in a URL.
- Turning remote access on or off is one command.

## 3. Non-goals

- Exposure beyond the tailnet (no Funnel, no public hostname).
- Multiple users, or per-device permissions.
- Tailscale identity headers as an auth mechanism (Jesinia's own auth still applies).

## 4. Design

### 4.1 Network shape

Uvicorn keeps binding `127.0.0.1`. `tailscale serve` terminates HTTPS on the tailnet name
(with Tailscale's certificate) and proxies to `http://127.0.0.1:<port>`, keeping the
original `Host`. Nothing new listens on a network interface.

### 4.2 Config

`[server] remote_host` holds the tailnet name, e.g. `mac.tail1234.ts.net` (lower-case DNS
name; no scheme, port or path). Empty means remote access is off.

### 4.3 Host and Origin guard

With `remote_host` set, `LocalOriginGuard` also accepts `Host: <remote_host>` and
`<remote_host>:443`, and the write origin `https://<remote_host>`. Responses to those
requests also carry `Strict-Transport-Security: max-age=31536000`. Every other host still
gets 421.

### 4.4 Cookies

A session cookie set or renewed on a request for the remote host carries `Secure`. Local
cookies are unchanged (Safari drops `Secure` cookies on plain-http localhost).

### 4.5 Signing in another device

`POST /api/auth/nonce` also returns `remote_url`, the login link on
`https://<remote_host>`, or `null` when remote access is off. `jesinia login --remote`
prints it; `jesinia status` prints it after the local link. The nonce stays single-use
and expires after 5 minutes.

### 4.6 CLI

- `jesinia remote enable`: finds the Tailscale CLI (`tailscale` on `PATH`, else
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale`), reads this machine's name from
  `tailscale status --json` (`Self.DNSName`), runs
  `tailscale serve --bg --https=443 http://127.0.0.1:<port>`, saves `server.remote_host`,
  and restarts the LaunchAgent.
- `jesinia remote disable`: `tailscale serve --https=443 off`, clears the key, restarts.
- `jesinia remote status`: the configured host, or that remote access is off.
- `jesinia config set server.remote_host …` works too, for a machine whose proxy is set up
  by hand.

## 5. Left to the builder

Error wording, how the CLI reports a missing Tailscale install or a logged-out node.
