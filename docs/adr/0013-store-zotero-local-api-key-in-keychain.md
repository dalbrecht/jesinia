# ADR 0013: Store the Zotero local API key in the macOS Keychain

- **Status:** Accepted
- **Date:** 2026-10-23
- **Related:** [security conventions](../conventions/security.md);
  [ADR 0004](0004-files-are-source-of-truth.md)

## Context

The Zotero integration reads and writes the owner's library through Zotero 10's local API
on `127.0.0.1:23119`. Reads need no credential; writes need a local API key that Zotero
issues once the owner approves a dialog ("Always Allow") for the app named Jesinia. Until
now Jesinia stored no credentials at all, and the key has to survive restarts, or the owner
would face the dialog again on every write.

## Decision

- **Keep the key in the login Keychain**, service `jesinia-zotero`, account `local-api`,
  and nowhere else. It is not in `config.toml`, the KB, logs or the database.
- **Write it through `security -i` on stdin**, so it never appears in argv or `ps`.
  Read it with `security find-generic-password ... -w`. Exit code 44 means "no item".
- **Store only a remembered key.** A single-use key ("Allow" instead of "Always Allow")
  is discarded and Settings tells the owner to choose "Always Allow".
- **Never log the key** or put it in an exception message.

## Consequences

This is the single exception to "Jesinia stores no credentials". The key is scoped to the
local machine (the API listens on loopback only, and the client refuses any other host),
and the owner can revoke it any time in Zotero Settings, Advanced; a revoked key makes the
next write report `unauthorized` and Settings offers to authorize again. Keychain access
is prompted by macOS for other binaries, so a process that is not Jesinia's server cannot
read it silently.
