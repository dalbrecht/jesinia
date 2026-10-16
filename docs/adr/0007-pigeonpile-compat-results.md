# ADR 0007 — Pigeonpile Wiki compatibility results

- **Status:** Accepted (provisional — pending the suite-side cross-test)
- **Date:** 2026-10-16
- **Related:** foundation spec §5.6; plan Task 1; fixture `docs/design/compat/`;
  suite-side cross-test tracked in c65llc/amicus-platform#3212

## Context

Jesinia shares a KB folder with the Pigeonpile Wiki through the on-disk corpus format
only (ADR 0003). Two behaviors were unverified: whether the Wiki preserves unknown
front-matter keys (Jesinia's `jesinia:` block) on rewrite, and whether its renderer hides
HTML comments (Jesinia's `<!-- jesinia:ai … -->` provenance markers).

## Decision

A local check of the fixture against the Pigeonpile Wiki **passed**. Jesinia proceeds
with the primary design:

- Jesinia metadata stays in the `jesinia:` front-matter block (no sidecar fallback).
- AI provenance stays as `<!-- jesinia:ai … -->` / `<!-- /jesinia:ai -->` comments.

The result is provisional: the suite has no test guaranteeing this behavior, so a
cross-test is tracked on the suite side to keep it from regressing.

## Consequences

- Plan Task 10 Step 5 (sidecar fallback) is not needed unless the cross-test later fails.
- If a future suite change breaks either behavior, revisit this ADR; the fixture in
  `docs/design/compat/` is the reproduction.
