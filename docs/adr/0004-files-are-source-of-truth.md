# ADR 0004: The Markdown files are the source of truth

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §3.4, §4.1, §5.4

## Context

The KB is edited by Jesinia, by the Pigeonpile Wiki, by text editors, and by git pulls
from other machines. Any database copy of the content can go stale.

## Decision

- Pages live only as files in the user's KB folder. Every database is derived from them
  (the page index and search), a cache, or agent history.
- Writes are atomic: a temporary file, fsync, then rename. A write that names a base
  revision is rechecked just before the rename and refused with `StaleWrite` if the file
  changed (an external save always wins). Creates and moves never overwrite another
  file.
- Identity is the front-matter `id`. A rename or move found on disk is treated as a
  move, never as delete plus create.
- **No delete comes from a vanished file.** A missing file is reported as a conflict, not
  propagated. Jesinia never deletes or renames user files on its own.
- Derived databases can be rebuilt at any time (`jesinia reindex`).

## Consequences

- Losing `kb.sqlite` or `search.sqlite` costs a rescan, not data.
- Concurrent edits produce a visible conflict (`StaleWrite`, or the Notes changed-on-disk
  bar) instead of silent loss.
- Duplicate ids (for example, copies made by a sync tool) are surfaced for the user to
  fix rather than auto-resolved.
