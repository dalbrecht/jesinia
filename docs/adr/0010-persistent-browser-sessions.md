# ADR 0010: Browser sessions survive restarts

- **Status:** Accepted
- **Date:** 2026-10-19
- **Related:** foundation spec §6, §7.4; [ADR 0009](0009-install-and-operations.md);
  [security conventions](../conventions/security.md)

## Context

Spec §7.4 kept browser sessions in process memory, and the cookie had no expiry. With
keep-alive and auto-update (ADR 0009), the server restarts often, and every restart signed
out every browser. Getting back in meant running `jesinia status`, copying the link and
pasting it within five minutes, which is awkward in an installed app window with no
address bar.

## Decision

- **Persist sessions** in `sessions.json` in the data dir, mode 600, written atomically.
  The file maps the SHA-256 of each cookie value to its expiry. It never holds the cookie
  values, so reading the file alone cannot sign anyone in.
- **30-day sliding expiry.** The cookie carries `Max-Age` of 30 days. A session in use
  renews at most once a day: the server extends the expiry and re-sends the cookie.
- **Rotation still signs everyone out**, on disk too, so a restart does not bring old
  sessions back.
- **Best-effort persistence.** A failed save is logged and the session keeps working in
  memory until the next restart. An unreadable file is logged and ignored, so everyone
  signs in again. Neither case blocks sign-in.
- **`jesinia login`** mints a nonce and opens the link in the default browser (`--print`
  prints it). The signed-out banner points to it.

## Rejected

- **Trust loopback requests without credentials.** Any local process, and any web page
  that gets past the Host check, would have full access.
- **The install token in a URL or a long-lived link.** URLs leak through history, logs and
  process lists.

## Consequences

A stolen cookie value stays valid for up to 30 days unused, or until the token is rotated.
Before, it was valid until the next restart. The cookie is still `HttpOnly;
SameSite=Strict` and only sent to the local origin.
