# KB format

The on-disk rules for a Jesinia knowledge base. They match the Pigeonpile suite's corpus
format, so both tools can share one folder (ADR 0003). This file is the canonical
statement; the agent skills cite it.

## Files and paths

- A KB is a folder of Markdown (`.md`) files. Nested folders mirror the tree:
  `Reading/Feeds/Some Post Title.md`.
- **Title as path.** The file name is the page title, spaces preserved. To make a title
  safe for disk, Jesinia:
  - normalizes it to NFC;
  - replaces `/`, `\`, `:` and control characters with a space;
  - collapses runs of spaces;
  - strips trailing dots and spaces.

  An empty result becomes `Untitled`. Sanitizing never changes the page's identity.
- **Collisions.** When two pages in the same folder have the same title (compared
  case-insensitively), the page with the smaller `id` keeps the bare name. Each other
  page gets the suffix ` [<id-prefix>]`: a space, then the first 8 hex characters of its
  `id` without dashes, in square brackets (`Same.md`, `Same [01a145d8].md`). Never a bare
  ` 2`. If that name is also taken, the full id is used.
- **Reserved folders.** `Journal/` and `Drafts/` (top level, any case) belong to the
  Pigeonpile suite. Jesinia never writes inside them, and never writes in hidden folders
  (any path segment starting with `.`) or `_assets/` folders.
- **macOS file names** may arrive in NFD. Jesinia matches pages by `id`, so an NFD file
  is never duplicated or renamed on each scan.

## Front matter

```yaml
---
id: 0199e000-0000-7000-8000-000000000001
title: Some Post Title
aliases: [sp]          # foreign keys are kept as they are
jesinia:
  kind: note           # note | source/rss | review/pr | review/system | …
  captured: 2026-10-16T09:12:00-07:00
---
```

- `id` is a UUIDv7, lowercase and dashed. It is the page's identity; the path is only a
  display artifact. Moves and renames found on disk are moves, never delete plus create.
- `title` is written bare when it round-trips as a plain YAML scalar, and JSON-quoted
  otherwise (`"yes"`, `"a: b"`, `"123"`).
- **Order:** `id`, `title`, any foreign keys in their original order, `jesinia` last.
- Jesinia's own metadata lives **only** under `jesinia:`. Anything derived (read state,
  transcripts, scores) lives in SQLite, keyed by `id`.
- A body-only edit (the Notes editor, `PATCH …/body`) keeps the front-matter block
  **byte-for-byte**: comments, flow style, quoting and key order are never re-serialized.

## What counts as a page

| On disk | Treatment |
|---|---|
| `.md` with front matter that has an `id` | A page |
| `.md` without front matter | **Adopted**: an `id` is minted, the title comes from the file name, and front matter is written back (only if the file didn't change meanwhile) |
| front matter without `id` (Jekyll and similar) | Ignored, never rewritten |
| other files, hidden paths, `_assets/` | Ignored |
| two files with the same `id` | A *duplicate id* conflict, shown in Settings; neither is deleted |
| an indexed file that disappears | A *missing* conflict; nothing is deleted from anywhere |

## Body

- **Links:** `[[<id>|<Title>]]` or `[[<id>]]`. They resolve by id, so they survive moves.
  Renaming a page updates the title text of links to it.
- **Tags:** inline `#tags` in the body; see [Tags](#tags).
- **Assets:** `_assets/<page-id>/` at the KB root, referenced by relative path (from
  `Sources/x.md`, `../_assets/<page-id>/<file>`). A folder whose files stay on this Mac
  (archived RSS images) holds a `.gitignore` of `*`, so KB sync never commits them. Asset
  file names are derived (a content hash plus an extension), never taken from a URL.
  The server serves them, signed in, at `/api/kb/assets/<page-id>/<file>`: raster images
  only (PNG, JPEG, GIF, WebP, AVIF, sniffed from the bytes; SVG is refused), never through
  a symlink. Notes' Read view shows them from there.
- **AI provenance.** Text an agent wrote is wrapped in Markdown (HTML) comments,
  exactly:

  ```markdown
  <!-- jesinia:ai agent=<agent> model=<model> at=<ISO-8601 UTC, Z> session=<id> -->
  …agent-written text…
  <!-- /jesinia:ai -->
  ```

  For example `<!-- jesinia:ai agent=claude-code model=claude-opus-5-5
  at=2026-10-16T09:20:00Z session=3f2c… -->` (on one line). The comments are advisory:
  the user may edit or merge the text freely. Jesinia neutralizes comment markers inside
  saved text so a note can't forge a block, and the Notes editor shows the markers as
  read-only chips. Agents never write the markers themselves: `notes_save` adds them,
  and so does `kb_write` when an agent creates a page with `provenance: {agent, model}`
  (the server stamps `at` and `session`, which is `terminal` outside a Jesinia session).

## Tags

### Grammar

- A tag is `#` followed by a letter or digit, then any letters or digits, joined by single
  `-` (`#sqlite`, `#wal-mode`, `#2026`). Unicode letters and their combining marks count
  (`#ünïcode`, composed or decomposed); `_` does not. A trailing `-` is not part of the tag
  (`#foo-` is `#foo`).
- The `#` must not follow a word character, `/`, `#`, `?`, `=` or `&`, and must not be a
  Markdown link target (`](#a)`). So `C#`, URLs, link anchors (`[x](#a)`), query strings
  (`?q=#a`), entities (`&#38;`) and `##` are never tags; `(#a)` in prose is.
- A tag must not run on into `_` or another word character: `#machine_learning` and `#b_c`
  are not tags at all (and renaming `#machine` never touches them).
- Ignored: fenced code blocks, indented code blocks, inline code, and heading lines
  (`# Title` is a heading, not a tag).
- Tags are case-insensitive and compared in NFC: `#SQLite` and `#sqlite` are one tag.
  Jesinia writes new tags in lowercase, with spaces and `_` turned into `-`.

### The `Tags:` line

Jesinia puts the tags it adds (for example on Save note) on one line of their own, after
the user's text:

```markdown
Tags: #sqlite #wal-mode
```

It is ordinary body text: tags anywhere else in the body count the same.

### The catalog page

`Tags.md` at the KB root (front matter `jesinia: {kind: tag-catalog}`) is the KB-wide list
of approved tags. It is an ordinary page; Settings → Tags edits it, and so can the user.
One line per tag:

```markdown
- #sqlite: The SQLite database (aliases: sqlite3, sql-lite)
- #wal-mode
- #old-name: Kept on pages, no longer suggested (retired)
- #todo (ignored)
```

- `: meaning` is optional. `(aliases: a, b)` lists other spellings that resolve to this
  tag (a rename or merge records the old name as an alias).
- `(retired)` keeps the tag valid on pages but stops suggesting it. `(ignored)` marks a
  tag used in pages that is deliberately left out of the catalog; it is never suggested.
- Any other line (headings, prose) is kept as written.
- Duplicate entry lines for the same tag are merged when read (the first non-empty meaning
  wins, aliases are combined) and rewritten as one line on the next catalog write.
- Agents check the catalog (`tags_suggest`, `tags_check`) before writing tags, and never
  create a near-duplicate of an existing tag. Those MCP tools answer from the catalog alone;
  the calling agent judges meaning itself.

## Writing safely

- Every write is atomic: temp file `*.jesinia-tmp`, fsync, then rename. Git sync never
  stages the temp files.
- An update names the revision it is based on (`expected_rev`, the sha256 of the file).
  If the file changed, the write fails with `StaleWrite` and nothing is overwritten.
  Re-read, merge and retry.
- An update through `kb_write` carries the page's `jesinia` metadata from `kb_read`.
  Omitting it keeps the existing metadata; an explicit `{}` clears it.
- Creates and moves never replace an existing file.

## Pigeonpile compatibility

ADR 0007 records the check against the Pigeonpile Wiki: it preserves the `jesinia:` block
on re-save and hides the provenance comments when rendering. A suite-side test pins this
behavior. Changes to this format need the same check before they ship.
