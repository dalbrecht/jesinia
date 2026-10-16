---
name: jesinia-kb-conventions
description: Use when reading, searching, writing, moving or linking pages in the user's Jesinia knowledge base (any kb_* tool, or a request about their notes, wiki or KB). Explains the page format, folders, links, tags, provenance and safe concurrent edits.
---

# Working in a Jesinia knowledge base

The KB is a folder of Markdown files the user owns. Jesinia indexes it and exposes it
through MCP tools. The full format is in `docs/conventions/kb-format.md` in the Jesinia
repository; this skill is the working summary.

## Always use the tools

- Find pages with `kb_search` (KB only) or `search_query` (KB plus app records). Browse
  with `kb_tree`. Read with `kb_read`.
- Write with `kb_write`, move or rename with `kb_move`. **Never edit KB files directly**,
  even if you can see the folder: the tools keep ids, links, the index and git sync
  correct.
- Writes from a Jesinia chat session usually need the user's approval in the app. Say
  what you are proposing and wait; don't retry a write that is awaiting approval.

## Pages

- Each page has front matter with an `id` (a UUID) and a `title`. Never invent or change
  an `id`.
- The file name follows the title. Jesinia handles collisions by adding an id suffix, so
  never pick file names yourself.
- `jesinia:` holds Jesinia's own metadata (for example `kind: note`). Leave other
  front-matter keys alone; Jesinia preserves them byte-for-byte.

## Folders

- Put pages in an existing topic folder when one fits (`kb_tree` shows them). Notes go
  in `Notes/` (configurable).
- Never write in `Journal/`, `Drafts/`, hidden folders, or any `_assets` folder.

## Links and tags

- Link to another page as `[[<id>|<Title>]]`. Links resolve by id, so they survive
  renames. Get the id with `kb_search` or `kb_link`; a rename updates the link text for
  you.
- Tags are inline `#tags` in the body. Reuse existing tags (search for them) before
  inventing new ones.

## Provenance

Text you write goes inside a provenance block, which Jesinia adds when it records your
words (for example through `notes_save`):

```
<!-- jesinia:ai agent=<agent> model=<model> at=<UTC time> session=<id> -->
…your text…
<!-- /jesinia:ai -->
```

Keep existing provenance blocks intact when editing a page. Never write these markers by
hand, and never wrap the user's own words in them.

## Safe updates

1. `kb_read` the page and keep its `rev`.
2. `kb_write` with `id` and `expected_rev` set to that `rev`.
3. If the write fails with `StaleWrite`, the user changed the page meanwhile. Read it
   again, merge your change into the new text, and write with the new `rev`. Never
   overwrite their edit.
