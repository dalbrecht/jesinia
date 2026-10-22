# ADR 0011: Agent-only pages and folders

- **Status:** Accepted
- **Date:** 2026-10-22
- **Related:** [KB format](../conventions/kb-format.md#agent-only-pages-and-folders);
  [ADR 0003](0003-format-only-pigeonpile-interop.md); [ADR 0004](0004-files-are-source-of-truth.md);
  [ADR 0007](0007-pigeonpile-compat-results.md)

## Context

The KB holds folders that belong to agents rather than the user: persistent memory,
scratch work, scripts. The user wants the Notes app to browse and edit the whole KB but
not these folders, while agents keep reading and writing them through MCP. Jesinia had no
way to say "agents only": every indexed page showed up in the tree and in search.

## Decision

- **The marker is front matter under `jesinia:`.** `access: agents` makes a page
  agent-only. Only that exact value locks, so a typo never half-hides a page.
- **A folder is locked by its lock page.** A page with `kind: folder` and `access: agents`
  locks every page in its folder and all subfolders, including pages created later. A lock
  page at the KB root locks only itself, so one page can never hide the whole KB.
- **The index records locks.** `kb.sqlite` gains `page_access(page_id, scope)`, filled from
  each page's front matter on write and scan like `page_sources`. The migration invalidates
  every mtime so the next scan fills it for existing pages.
- **Locks apply to the user's views only.** The `/api/kb` routes leave locked pages out of
  the tree and refuse to read, edit, move, serve assets for or create pages in them
  (`403 AgentOnlyPage`). `GET /api/search` and Notes' related pages leave them out, and
  Notes refuses to start a session about one. Writes the apps make for the user (a Notes
  save, an RSS keep, a kept review or "How it works") go through `KbService` with
  `for_user=True`, which applies the same refusals, and a user rename skips agent-only
  linkers rather than retitling their links. The MCP tools (`kb_*`, `kb_search`,
  `search_query`) are unchanged: agents see everything.
- **Paths compare case-insensitively**, matching the Mac's file system.

## Rejected

- **A folder-level dotfile** (`91 Memory/.jesinia`). Hidden files are outside the page
  format, the scanner skips them, and nothing else in the KB uses them; the user's KB
  already gives each folder an index page, so the marker can live there.
- **A list of folders in `config.toml`.** It would not travel with the KB through git sync,
  and it would not survive a folder rename. A lock page moves with its folder.
- **Matching an index page by title** (`Memory.md` in `91 Memory`). Too implicit: renaming
  a page would silently unlock a folder. `kind: folder` says it outright.
- **Hiding locked pages from agents' search too.** Agents are the folders' users.

## Consequences

- Pigeonpile preserves the `jesinia:` block on re-save (ADR 0007), so the marker survives
  edits there; the suite itself does not enforce it.
- This is a view rule, not a security boundary: anything that reads the files directly,
  any agent, and tag maintenance across the KB (Settings → Tags) still sees the pages.
- Unlocking is an edit outside the Notes app (an editor, or an agent through `kb_write`),
  since the lock page itself is locked.
