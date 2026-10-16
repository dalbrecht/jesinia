# ADR 0003: Interoperate with Pigeonpile through the file format only

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §2, §5; [KB format](../conventions/kb-format.md);
  [ADR 0007](0007-pigeonpile-compat-results.md)

## Context

The user already keeps a knowledge base that the Pigeonpile Wiki reads and writes.
Jesinia must share that folder without depending on Pigeonpile's code, APIs or styling
(it is proprietary).

## Decision

Jesinia and Pigeonpile share **only the on-disk corpus format**:

- Markdown files, with titles as paths.
- Front matter with `id` and `title`, plus a namespaced `jesinia:` block.
- `[[id|Title]]` links and `#tags`.
- Reserved `Journal/` and `Drafts/` folders.
- Provenance as HTML comments.

There are no code imports, no shared database and no network calls between them. The
format is specified in `docs/conventions/kb-format.md`, and its compatibility was
verified (ADR 0007).

## Consequences

- Either tool can be used alone; the other sees ordinary Markdown.
- Jesinia must preserve what it doesn't own: foreign front-matter keys, the raw YAML of
  body-only edits, `Journal/` and `Drafts/`.
- Format changes need a compatibility check against the Wiki before they ship.
