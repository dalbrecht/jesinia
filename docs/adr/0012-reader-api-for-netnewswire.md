# ADR 0012: The Google Reader API for NetNewsWire

- **Status:** Accepted
- **Date:** 2026-10-22
- **Related:** [NetNewsWire sync spec](../specs/2026-10-22-netnewswire-sync-design.md);
  [RSS reader spec](../specs/2026-10-16-rss-reader-design.md);
  [ADR 0011](0011-remote-access-over-tailscale.md); [security conventions](../conventions/security.md)

## Context

The owner wants NetNewsWire on iPhone and iPad as the main way to read Jesinia's feeds,
with Jesinia staying the source of truth. The RSS reader spec listed syncing with other
readers as a non-goal; this reverses that. NetNewsWire can sync with a self-hosted server
only through its FreshRSS account, which speaks the Google Reader API.

## Decision

- **Serve the Google Reader API subset NetNewsWire calls**, at `/api/greader`, inside the
  RSS app. Read state, Keep and feed management go through the reader's own services.
- **A star is a Keep; an unstar is only recorded.** An append-only star ledger in
  `rss.sqlite` records every keep and unkeep with its source; the starred stream is the
  posts whose latest entry is a keep.
- **Separate credentials.** A reader password (only its SHA-256 stored) is exchanged at
  `ClientLogin` for reader tokens (hashed too) that reach only the Reader API. Writes also
  need a write token derived from the reader token. Creating a password revokes the old
  one and its tokens. The routes are mounted outside the owner-auth dependency through a
  `PublicRoutes` module protocol, so each one checks its caller.
- **Full articles are prefetched** by a minute job, and new posts are held back from item
  lists for up to 15 minutes until theirs has been tried, because NetNewsWire downloads an
  item's content once.

## Rejected

- **Feedbin or Fever APIs.** NetNewsWire's Feedbin account has a fixed address; it has no
  Fever account.
- **The install token as the NetNewsWire password.** It grants everything, including MCP;
  a phone's keychain should hold something that reaches only the feeds.
- **Unstar as un-keep.** The owner wants the KB page and the archive kept; the ledger
  keeps the history instead.

## Consequences

A second credential exists, scoped to the Reader API. A leaked reader token can read the
feeds and change read state, stars and subscriptions until the password is replaced.
NetNewsWire's own Reader View still extracts articles through Feedbin's service; Jesinia's
prefetched article makes it unnecessary.
