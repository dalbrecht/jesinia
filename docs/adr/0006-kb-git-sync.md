# ADR 0006: KB git sync policy

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §4.4; [KB sync conventions](../conventions/kb-sync.md)

## Context

The user's KB is a git repository pushed to a remote so other machines (and the Wiki)
see changes. Jesinia writes to the KB, so it should keep the repository in sync, without
ever destroying work or surprising the user.

## Decision

- **Detect, don't configure.** A repository with a remote gets commit and push, a
  repository without a remote gets commit only, and a folder that isn't a repository
  gets no sync. Initializing git is an explicit button, never automatic.
- **Cycle** (every 4 hours by default, on demand, and at shutdown):
  1. Skip entirely when the tree is clean and not ahead.
  2. Wait for 30 s of quiet.
  3. Stage everything except Jesinia's temp files.
  4. Commit with a summary and the trailer `Jesinia-Auto-Sync: true`.
  5. `pull --rebase --autostash`.
  6. Push.
- **Never force-push, never auto-resolve.** A rebase conflict, or conflicted files left
  by the autostash, aborts the rebase and pauses sync until the user resolves it and
  chooses Resume.
- An odd state (detached HEAD, a rebase or merge in progress) blocks the cycle without
  touching the repository.
- Failures back off exponentially (5 min, doubling, capped at the interval), and errors
  are recorded with credentials redacted.

## Consequences

- The remote is never rewritten by Jesinia.
- A conflict is a visible, recoverable pause (a banner in every app) rather than a
  silent merge.
- Commits made by Jesinia are identifiable by their trailer.
