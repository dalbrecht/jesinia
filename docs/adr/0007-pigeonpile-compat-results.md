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

## Observed results

The 2026-10-16 local check was reported as an overall pass, not question by question.
The table below lists each check from `docs/design/compat/README.md` so the baseline is
explicit. Rows marked *not individually recorded* should be filled in with the exact
observation (and, ideally, the re-saved file committed under
`docs/design/compat/results/`) on the next run. The suite-side cross-test
(c65llc/amicus-platform#3212) will pin these as CI assertions.

| # | Check | Expected | Observed (2026-10-16) |
|---|---|---|---|
| a | `jesinia:` front-matter block preserved on re-save | preserved byte-for-byte | pass (overall) — not individually recorded |
| b | foreign key `aliases` preserved | preserved | not individually recorded |
| c | `<!-- jesinia:ai … -->` hidden when rendered and preserved on disk | hidden + preserved | pass (overall) — not individually recorded |
| d | `[[id\|Title]]` renders as a working link | link to Link Target | not individually recorded |
| e | `#compat` recognized as a tag | tag facet | not individually recorded |
| f | `Plain File.md` adopted | `id` + `title: Plain File` added, body unchanged | not individually recorded |
| g | no file renamed or moved | paths unchanged | not individually recorded |
| h | tag grammar (`Ideas/Tag grammar.md`): case folds; hyphens, digits and Unicode accepted | `#SQLite` = `#sqlite`; `#wal-mode`, `#2026`, `#ünïcode` whole | *pending manual check* |

The design decisions below depend only on (a) and (c).

### Addendum: tag grammar (row h)

Added with the KB-wide tag catalog. Row (h) is *pending manual check*: the author confirms
in the Wiki whether tags fold case and whether hyphens, digits and Unicode letters are
accepted. If any of them is not, Jesinia's grammar (`docs/conventions/kb-format.md#tags`)
is narrowed to match, with a follow-up issue if needed.

## Consequences

- Plan Task 10 Step 5 (sidecar fallback) is not needed unless the cross-test later fails.
- If a future suite change breaks either behavior, revisit this ADR; the fixture in
  `docs/design/compat/` is the reproduction.
