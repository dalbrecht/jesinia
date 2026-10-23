# Jesinia — Zotero Integration

- **Status:** Draft
- **Date:** 2026-10-23
- **Author:** Don Albrecht (with Claude)
- **Builds on:** the foundation spec (`2026-10-16-jesinia-foundation-design.md`), ADR 0002
  (single server, shared services), ADR 0004 (files are the source of truth), ADR 0005
  (CLI-driven agents), and the conventions in `docs/conventions/` (kb-format tags, agents,
  security, adding-an-app).
- **Mockups (throwaway):** `paper-inbox.html` from the design conversation, served from a
  temporary directory and not kept. It is described in full in §5.

Decisions marked **(user)** came from the author in the design conversation. Decisions marked
**(builder's call)** were made while writing this down and are open to change in review.

## 1. Intent

The author reads and manages papers in Zotero, and everything learned there stays there:
notes and highlights never reach the KB, papers can't be found from Jesinia search, and the
library is poorly organized. Tags are sparse, related papers aren't linked, and papers saved
from arXiv often have a bare arXiv number (`2403.01234`) as their title.

Jesinia becomes a layer over the Zotero library that:

1. **knows about every paper** (metadata, tags, the author's notes, PDF highlights and
   comments, and the PDF's full text) and surfaces it in Jesinia search and to agents over
   MCP;
2. **improves the library's organization**: Claude proposes tags, related-paper links, title
   and metadata fixes, and an explanatory note, and **the author approves each proposal in an
   inbox** before anything changes in Zotero;
3. **lets KB pages link to papers**, with a link that opens the paper in Zotero.

Zotero stays the primary place for reading and managing papers **(user)**, at least until a
better app exists. Reviewing the proposals is part of the point: "it is good for my brain to
see these things" **(user)**, so the inbox is a feature in its own right, not only a safety
gate.

### Success

- A paper added to Zotero is searchable in Jesinia, full text included, and has an inbox item
  waiting the same day **(user: "sometime today is fine")**.
- An arXiv-number title becomes the real title, authors, year and abstract after one approval.
- Approving a paper's inbox item puts the chosen tags, two-way related links and Claude's note
  into Zotero, and they sync to zotero.org the normal way.
- Jesinia search and the MCP tools return paper snippets, and an agent can pull a paper's full
  text into a chat.
- Papers and KB pages share one tag vocabulary **(user)**.

### Non-goals (for now)

- A per-paper page in the KB. A paper link opens the paper in Zotero **(user)**.
- Reading PDFs inside Jesinia, or replacing Zotero.
- Periodic re-review of papers already reviewed. That's a future feature **(user)**; the
  design shouldn't block it (§5.6).
- A bulk-tagging screen. Bulk tagging around a single tag happens in a conversation with
  Claude and is infrequent **(user)**. The one bulk item in the inbox is the tag-rename item
  (§6.3).
- Group libraries. Only the user's personal library (builder's call).
- Working when the Zotero desktop app is closed. No zotero.org web API and no stored
  zotero.org API key (builder's call, §3).
- Collections. Proposals don't move papers between collections (builder's call).

### Anti-goals

- **Deleting anything the author collected or wrote** **(user)**: Jesinia must never delete
  a Zotero item, attachment, PDF, note or annotation, and must never edit a note or
  annotation it didn't create. The only removals allowed are of a tag from an item, and only
  as part of an approved tag rename or merge (§6.3).
- **Changing Zotero without approval.** Every write to Zotero comes from an approved inbox
  item. No exceptions, including tag renames **(user)**.
- **Writing to `zotero.sqlite` directly.** Zotero's own guidance says this can corrupt the
  database and the edits don't sync. Jesinia may read a copy of it, but never writes to it.
- **Claude's guesses passed off as facts.** Title and metadata fixes come from arXiv (or
  another authoritative source), never from the model's memory.
- **Duplicating the tag vocabulary**, or near-duplicate tags such as `llm` and `llms`.

## 2. Current state (verified 2026-10-23)

**Zotero on the author's Mac:**

- Zotero **10.0.6**, with its data directory at `~/Zotero`, synced from zotero.org. The library
  is "rather small currently" but "will grow soon" **(user)**. On 2026-10-23 it held (counted
  from a read-only copy):
  - **102 standalone PDFs with no parent item.** Their titles are bare arXiv ids
    (`2510.15061`, `2506.18559v1`) or file names (`the-illusion-of-thinking.pdf`). This is
    the main "fix the title" case, and most of the library.
  - 19 regular items: 10 preprints, 7 web pages, 1 journal article and 1 program. Some web
    pages are really papers saved by URL (`[2402.12354] LoRA+: …`, a ResearchGate PDF URL as
    the title).
  - 9 PDFs attached to a parent item. 11 items have a DOI.
  - 31 tags: 33 manual tag uses and 28 automatic. There are 1 collection, 0 related links,
    0 notes and 0 annotations.
  - Only 42 PDFs downloaded to `storage/` so far, and `fulltext.sqlite` has nothing indexed
    yet. Full text will appear as Zotero downloads and indexes the files, so sync must
    handle full text that turns up later.
- Full text in Zotero 10 lives in a separate `fulltext.sqlite` (FTS5), not in the old
  `.zotero-ft-cache` files.
- No plugins are installed (no Better BibTeX).
- The **local HTTP API** (`http://127.0.0.1:23119/api/…`) is **off** (403 "Local API is not
  enabled"). It has to be turned on in Settings → Advanced → "Allow other applications on
  this computer to communicate with Zotero".
- According to Zotero's docs and forum (zotero.org/support/dev/web_api/v3/local_api), the
  local API in Zotero 10 **supports writes**:
  - POST, PUT, PATCH and DELETE on items.
  - Writes need a local API key, obtained with `POST /api/local/authorize`. The user approves
    it in a Zotero dialog, and "Always Allow" makes the key persistent.
  - Every write needs a `Zotero-Server-ID` header.
  - Updates are version-checked. A stale write gets 412.
  - Array fields such as `tags` and `relations` replace the whole set, so a write must read
    the item, merge, then write.
  - Changes sync to zotero.org on Zotero's next sync.
- Annotations are child items of the PDF attachment (`itemType: annotation`, with
  `annotationText`, `annotationComment`, `annotationPageLabel` and so on). Full text is read
  from `/items/<KEY>/fulltext`.

**Jesinia:**

- **Search** (`services/search`) is SQLite FTS5 with snippets and bm25 ranking. It already
  accepts outside documents through
  `SearchService.register(ExternalRecord(source, key, title, body, url))` and `unregister`,
  but no caller uses that yet. Nothing in the web shell opens a non-KB hit's `url` yet.
- **The review app is GitHub-PR review, not a general approval queue.** The paper inbox is
  new (§5).
- **Agent write approval:** a `ToolDef` with `writes=True` goes through `SessionWriteGate` /
  `PendingWrite`. That gate covers a single agent call inside a chat session. It isn't the
  paper inbox.
- **Agents** run as headless Claude Code or Codex. Their only built-ins are Read inside the
  session folder plus `mcp__jesinia__*`; they have no web access.
- **Tags:** `Tags.md` is the KB-wide catalog, with meanings, aliases, `(retired)` and
  `(ignored)`. It catches near-duplicates and already supports rename and merge. The read-only
  MCP tools are `tags_list`, `tags_suggest` and `tags_check`.
- **The RSS app** is the model for an app that ingests an outside source: its module layout,
  `ctx.scheduler.every`, its own SQLite store under the data dir, and a settings write to
  `config.toml`.
- Nothing in the repo mentions Zotero yet.

## 3. How Jesinia talks to Zotero

- **Reads and writes both go through Zotero 10's local API** (builder's call). This needs the
  desktop app running with the local-API setting on. No zotero.org web API and no stored
  zotero.org key.
- **The local API key** comes from the one-time `/api/local/authorize` dialog, where the author
  picks "Always Allow". Jesinia keeps the key in the macOS Keychain, following the GitHub
  precedent in `services/github/infrastructure/auth.py`. This stores a credential, which
  `docs/conventions/security.md` currently says Jesinia never does, so the builder writes an
  **ADR** for it. On a 401, Jesinia shows "re-authorize Zotero" in Settings. It never loops
  on authorize requests, which are rate-limited to 5 a minute.
- **When Zotero is closed:**
  - Sync pauses.
  - Approved changes wait in a queue and apply once Zotero is back.
  - Settings shows the state: connected, Zotero not running, local API off, or not authorized.
  - The inbox stays usable for deciding.
- **Every write:**
  - Read the item's current version.
  - Merge the change in. Tags and relations are full-set fields.
  - PATCH with the version.
  - On a 412, re-read, re-merge and retry once. If it fails again, mark the change failed and
    surface the cause; don't swallow it.
- **zotero.sqlite** is never written to. Reading a copy (including its `-wal`) is allowed only
  as a fallback if an API read is missing something (builder's call). The plan should not
  depend on it.

## 4. Sync: Zotero → Jesinia

- **What's pulled** for each regular item:
  - key, version, title, creators, year, abstract, DOI, URL, arXiv id (from URL, extra or
    title), item type, tags (with their manual/automatic type), and `dc:relation` relations
  - child notes
  - each PDF attachment's annotations (highlight text, comment, page label, color)
  - the PDF's full text
- **Incrementally:** each pass starts from the stored cursor `V` and reads four feeds, all
  `since=V`. The new cursor is committed only after all four have been processed:
  1. `/items?since=V` for changed items.
  2. `/items/trash?since=V` (or `includeTrashed=1`) for items moved to the trash, which count
     as deleted. The plain `/items` feed leaves these out.
  3. `/deleted?since=V` for permanent deletions (tombstones).
  4. `/fulltext?since=V`, the full-text change feed. For each attachment it lists, Jesinia
     fetches `/items/<KEY>/fulltext`. This catches text that Zotero indexes after the
     attachment itself synced (§2). Without it, those papers would never be searchable by
     full text.
  - Deletions and trashed items remove the paper from Jesinia's mirror and search index, and
    close its open inbox item as "paper gone". Jesinia never deletes anything in Zotero.
- **Server ID:** the cursor and the mirror are stored together with the `Zotero-Server-ID`
  they came from, as the local API docs require. A different server ID on any response means
  a different Zotero database (another profile, a replaced data directory, a restore). When
  that happens:
  - sync stops using the old cursor and rebuilds the mirror and search records from scratch;
  - pending outbox writes tied to the old server ID are quarantined, not sent. Their inbox
    items show "Zotero library changed" and can be re-proposed.
- **Schedule:** `ctx.scheduler.every` runs it on a light interval (builder's call, for example
  every 10 minutes). It also runs at start and on demand from Settings.
- **Store:** a new `zotero.sqlite` under Jesinia's data dir (not Zotero's). It is a mirror,
  rebuildable from Zotero. The inbox and the outbox of changes are history, since they hold
  the author's decisions. The builder picks the `ModuleDb` class(es) to match ADR 0004.
- **Error isolation:** sync is a secondary subsystem. A failure there never breaks the KB or
  search, and every failure keeps and shows its original cause.

## 5. The paper inbox

### 5.1 Shape (user)

- One inbox item per paper. Every proposal for that paper sits on that one item.
- Layout: a list on the left, the selected paper on the right (as in the mockup).
- New papers (from sync) get an item. So does the existing library when the integration is
  first turned on.
- **The backlog arrives in batches (user):** the existing library is fed in a batch at a time,
  so the inbox never floods. The builder sets the batch size (for example 10–20 papers per
  run). The design must hold up as the library grows.

### 5.2 What a paper item shows

- **Title fix:** the current title (struck through) and the proposed one, plus the authors,
  year, abstract and DOI that will be filled in.
  - The source is shown: "from arXiv" or "from Crossref".
  - **The author can edit the title** before applying **(user)**.
  - It only appears when the current title is an arXiv id, empty, a URL or a file name
    (builder's call on the exact test).
  - **Sources, in order (user):**
    1. arXiv, when an arXiv id is found in the title, URL, `extra` or file name.
    2. Crossref by DOI, when there is a DOI, or one is found in the PDF's text.
    3. Otherwise, no fix is offered. The item says so ("no source found"), and the author
       can still type a title.
- **Standalone PDF → proper paper (builder's call; most of today's library, §2):** for a PDF
  with no parent item, the fix **creates a parent item**, with the type taken from the source
  (preprint for arXiv, the Crossref type otherwise). It then moves the PDF under that item,
  as Zotero's own "Create Parent Item" does. Tags, related links and Claude's note go on the
  new parent, not on the PDF. Nothing is deleted.
- **A web page that is really a paper** (an arXiv or ResearchGate URL) gets the same fix: the
  item's type and metadata are corrected from the source (builder's call).
- **Tags:** Claude's proposals as ticked chips. A tag not yet in the catalog is marked
  **new**.
  - **The author can type in their own tags (user).** These are checked against the catalog
    (`tags_check`), so typing a near-duplicate offers the existing tag instead.
- **Related papers:** each proposal shows the other paper's title and a one-line reason. Only
  papers already in the library can be proposed.
- **Claude's note:** a short explanation of the paper and of why these suggestions fit. It is
  shown here and saved to Zotero on apply (§6.2).
- **Actions:**
  - **Apply checked** sends the ticked changes.
  - **Skip for now** leaves the item in the inbox.
  - **Reject all** closes the item and changes nothing.
  - **Discuss with Claude** opens an agent chat about this paper, with the item's proposals
    as context. **Claude can change the proposals during the chat (user):** swap tags,
    rewrite the note, add or drop related papers, or change the title fix.
    - It does this through a write tool (for example `zotero_revise`) that edits only the
      inbox item, never Zotero.
    - The item view updates live.
    - If the author is editing the same item, the later write wins only over the fields it
      touches. The builder picks how: a version per item, with a conflict on stale writes,
      to match the KB's `StaleWrite`.
    - Applying stays the author's click.

### 5.3 Deciding (user)

- **Everything starts ticked.** The author unticks what they don't want, adds or edits things,
  and applies once.
- Applying closes the item. Its changes go to the outbox (§6), and the item shows whether they
  landed in Zotero.

### 5.4 The tag-change item (user)

- A tag rename or merge in Jesinia creates its own inbox item: "ml → machine-learning, 23
  papers", with the papers listed. One approval rewrites the tag on all of them.
- Papers can be unticked individually (builder's call, matching §5.3).
- More in §6.3.

### 5.5 Where it lives (builder's call)

- A new **Papers** app in the shell, registered in `web/packages/shell/src/apps.ts`. The inbox
  is its main screen.
- Settings → Papers holds:
  - the connection state and re-authorize
  - sync now
  - the backlog batch size
  - the tag-creation setting (§7)

### 5.6 Room for re-review (future)

- Store which paper versions were reviewed, and when. A later "re-review" can then make a new
  inbox item for a paper already reviewed.
- Claude's note is updated in place rather than duplicated (§6.2).

## 6. Writing back: Jesinia → Zotero

All writes come from approved inbox items, through one outbox that queues changes and retries
them.

### 6.1 Allowed writes (exhaustive)

| Change | How |
|---|---|
| Title and metadata fix | PATCH title, creators, date, abstract, DOI, URL, arXiv id in `extra`, and `itemType` when the source says it's a paper (for example webpage → preprint, §5.2). Fields the new type doesn't support are left out of the PATCH, not cleared |
| Standalone PDF → paper | Create a parent item from the source's metadata, then PATCH the PDF's `parentItem` |
| Add tags | Merge into the item's `tags`. Tags Jesinia adds are manual tags (`type` 0) |
| Related link | Add `dc:relation` on **both** papers (§6.4) |
| Claude's note | Create one child note, or update the note Jesinia created earlier (§6.2) |
| Tag rename or merge | On each approved paper, replace the old tag with the new one |

Nothing else. In particular, no deletes, no edits to notes or annotations the author made, and
no attachment changes.

### 6.2 Claude's note

- A child note on the paper, clearly marked as Claude's work: a heading such as "Jesinia ·
  Claude's notes", the date, and the note text.
- Jesinia records the note's key. Later writes update that same note instead of adding another
  one.
- If the author edits the note in Zotero, it becomes theirs, and Jesinia never overwrites it
  again. It creates a new note if it ever needs to. Builder's call on how to detect an edit,
  for example a stored content hash.

### 6.3 Tag rename and merge (user)

- A rename or merge in the KB tag catalog creates the tag-change inbox item **on its own**,
  but nothing changes in Zotero until it's approved.
- After approval:
  - each ticked paper loses the old tag and gains the new one, in one version-checked PATCH;
  - unticked papers keep the old tag;
  - the old tag stays in the catalog as an alias. It isn't flagged as uncataloged again (the
    catalog resolves aliases). Re-approving the rename later is possible from the papers list
    (builder's call).
- **Triggers (builder's call):** any new alias that appears in `Tags.md`, whatever made it
  (Jesinia's rename/merge API, a hand edit, or a KB git sync pull), creates a tag-change item
  for the papers still carrying the old tag. Items are deduplicated by (old tag, new tag).
  The builder finds a way to connect this that respects the layering, for example an event
  from the kb service or a catalog diff on reload.

### 6.4 Related links are two-way (user)

- Applying a related link makes both papers show each other in Zotero.
- If Zotero doesn't add the reverse relation when the API writes one side, Jesinia writes the
  reverse itself, as part of the same applied change. The builder checks this against the
  real app first.
- *Unverified:* the desktop app treats related items as two-way, but an API write may only
  add one side.

### 6.5 Outbox

- Each applied item becomes one or more pending writes.
- They apply as soon as Zotero is reachable. If Zotero is closed, they wait.
- After a write, the inbox item shows the result: applied, waiting for Zotero, or failed with
  the cause and a retry button.
- Writes are idempotent, so re-sending an applied change does nothing (re-read, merge, no-op).

## 7. One tag vocabulary (user)

- Papers use the KB catalog `Tags.md`. A tag means the same thing on a paper and on a page.
- Claude proposes from the catalog first (`tags_suggest`).
- **While the KB is being bootstrapped, Claude is generous about creating new tags (user).**
  Every new tag is still an inbox decision.
  - Applying a new tag adds it to `Tags.md`, with a one-line meaning Claude drafted. The
    author can edit the meaning on the item (builder's call).
  - A setting (generous / conservative) controls this, starting at generous (builder's call).
- **Zotero's automatic tags** (`type` 1, such as "Computer Science - Robotics" from arXiv or
  publishers) aren't the author's vocabulary. Claude may read them as hints. Jesinia never
  adds them to the catalog or touches them in Zotero (builder's call).
- **Tags already on papers but missing from the catalog** are listed alongside the KB's
  existing uncataloged-tags list. From there the author adds, maps or ignores each one.
  Mapping one to an existing tag is a rename, and goes through §6.3 (builder's call).
  It uses the catalog's existing merge, so KB pages that carry the old tag are rewritten too.
- **Matching Zotero tags to catalog tags (builder's call):**
  - A Zotero tag matches a catalog tag after the KB's own normalization: case-insensitive,
    NFC, with spaces and `_` turned into `-`. So "Machine Learning" in Zotero *is*
    `#machine-learning`, and is left as it is in Zotero (no churn).
  - Tags Jesinia adds are written in the KB form (`machine-learning`).
  - A Zotero tag that can't become a valid KB tag (for example `RL/robotics`) is listed as
    uncataloged, and can be mapped through §6.3.

## 8. Making the proposals

- **Deterministic work runs on the server, not in the model:**
  - Spotting arXiv-only, URL and file-name titles, and finding arXiv ids or DOIs in the
    title, URL, `extra`, file name or the PDF's first page.
  - Fetching metadata through the shared `fetch` service: the arXiv API first, Crossref by DOI
    as the fallback (§5.2). Agents have no web access, which fits the "facts from the source,
    not memory" anti-goal.
  - Finding candidate related papers with search over the papers already indexed.
- **Claude's work (agents service, Claude Code by default):**
  - Choosing the tags.
  - Picking related papers from the candidates and writing each reason.
  - Writing Claude's note.
- **It runs in the background** on a schedule, in batches. "Sometime today" is fine **(user)**,
  so the builder picks the cadence, for example a few times a day plus once after a backlog
  batch is due.
- **The background agent has no write tools.** It runs in a `read_only` session (the review
  app's pattern), limited to the read tools `zotero_get`, `zotero_fulltext`, `zotero_search`,
  `tags_list`, `tags_suggest` and `tags_check`. Paper text arrives fenced as untrusted (§9).
- **It answers with JSON**, holding the tags (with meanings for new ones), the related keys
  and reasons, and the note. The server parses it against a schema (the issues app's
  `parse_agent_json` pattern, with one corrective retry). Then the server, which is trusted
  code:
  - validates it (related keys must exist in the mirror; caps on counts and lengths);
  - normalizes the tags;
  - adds the server-resolved title fix;
  - writes the inbox item itself.
  No agent write passes through `SessionWriteGate`, and nothing needs pre-authorizing.
- **No API bill:** the work uses the CLI's own login, the same way the existing agent runners
  do.

## 9. Search, MCP and KB links

### 9.1 Search

- **Each paper is one search record**, `source="zotero"` (builder's call on the name and on
  how notes and highlights split across records). It holds:
  - title and authors, in the title column
  - abstract, tags, the author's notes, highlight text and comments, and the full text, in
    the body
- The record's `url` is a `zotero://select/library/items/<KEY>` link. A hit whose best match
  is a highlight may open `zotero://open-pdf/library/items/<ATTACHMENT>?page=N&annotation=<KEY>`
  instead (builder's call; check these schemes against Zotero 10).
- **The web shell learns to open a non-KB hit's `url`.** Today it only routes KB hits. Hits
  are labeled as papers.
- Papers are never written into the KB as Markdown. They live in the search index and the
  Zotero store only **(user)**.

### 9.2 MCP tools

Named `zotero_<verb>`, with paper text fenced as untrusted the way RSS posts are
(`_fenced`).

**Read tools** (`writes=False`):

- `zotero_search`: snippets plus paper keys.
- `zotero_get`: metadata, tags, notes, highlights, related papers, and the link.
- `zotero_fulltext`: the PDF's text, paged if it's long.

**One write tool**, `zotero_revise` (`writes=True`, with a preview). It edits an inbox item's
proposals during "Discuss with Claude" (§5.2), and never touches Zotero.

- It is pre-authorized only in Discuss sessions. Those sessions start from the author's own
  click on that paper, and the author asked for Claude to change the suggestions there
  **(user)**.
- It can only edit the one inbox item its session was opened for, and the author's Apply is
  still the only path to Zotero.
- The builder adds this case to the pre-authorization rule in `docs/conventions/agents.md`
  ("Writes and approval"), next to Notes' `notes_save`.
- In any other session, it goes through the normal write gate.

Each result includes a ready-to-paste link, so agents can cite papers in KB pages (builder's
call).

### 9.3 KB links (user)

- A KB page links to a paper with an ordinary Markdown link to its `zotero://` URL. There is
  no new link syntax.
- The KB renderer and the shell must let `zotero://` links open (check the URL sanitizer).
  Clicking one opens the paper in Zotero.

## 10. Testing

- **The Zotero client sits behind a port.** Unit tests use a fake local API that covers:
  versions and 412s, full-set tags and relations, notes, annotations, full text, a 401 that
  needs re-authorizing, Zotero not running, and the local API being off.
- **Domain logic, at 100%:**
  - arXiv-id detection
  - proposal merge (ticked, unticked, author-added tags, edited title)
  - full-set tag and relation merges
  - the reciprocal-relation plan
  - the tag-rename plan
  - the Claude-note ownership rule
  - **the no-delete rule:** a test asserts the outbox can never produce a DELETE, or a PATCH
    that removes a tag other than the old tag of an approved rename.
- **Sync:** incremental `since`, deletions, and trash. Search records are registered and
  unregistered.
- **Web:** the inbox list and detail; untick, add and edit, then apply; the tag-change item;
  the outbox state on an item.
- **E2E:** a Playwright spec against the fake local API, going from a new arXiv paper, to its
  inbox item, to apply, to the PATCH that was sent.
- **Manual verification** before calling it done, on the real Zotero 10:
  - the authorize dialog
  - a real two-way related link
  - `zotero://` links from search and from a KB page
  - changes reaching zotero.org

## 11. First task: prove the writes on the real app

Writes through the local API are documented but haven't been tried. On 2026-10-23 the local
API still answered 403 (off). So the plan **starts with a short spike** against the author's
real Zotero 10, with the author present (it needs the authorize dialog). It works on **one
throwaway test item that Jesinia creates itself**, never on the author's papers, and tries:

- PATCH tags
- add a `dc:relation`, and check whether Zotero adds the reverse on the other item
- create a child note, then update it
- create a parent item for a standalone PDF and move the PDF under it
- the version check (412)

Removing the test item afterwards is left to the author, from Zotero's trash. Jesinia never
deletes it.

If a write turns out to be impossible, **stop and ask the author** before choosing a fallback.
The zotero.org web API with a Keychain-stored key is the obvious candidate, but the author
hasn't agreed to it.

## 12. Left to the builder

- Module boundaries:
  - a `services/zotero` client and store, if other apps will use it, plus an `apps/papers`
    for the inbox UI;
  - or a single `apps/zotero`.
- Endpoint shapes, store schema and migrations.
- Scheduling intervals and the batch size default.
- The inbox component split, CSS and copy, following `docs/design/`.
- The agent prompt for proposals.
- The JSON schema for the background agent's answer (§8).
- The ADR for storing the Zotero local API key (§3).

## 13. Before building: the author's setup

1. Sync Zotero (done 2026-10-23).
2. Turn on Settings → Advanced → "Allow other applications on this computer to communicate
   with Zotero".
3. When Jesinia first asks, approve its authorization in Zotero with "Always Allow".
