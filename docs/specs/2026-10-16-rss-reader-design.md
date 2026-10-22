# Jesinia — RSS Reader Design

- **Status:** Implemented (acceptance in §10)
- **Date:** 2026-10-16
- **Author:** Don Albrecht (with Claude)
- **Builds on:** the foundation spec (`2026-10-16-jesinia-foundation-design.md`, Implemented),
  ADR 0008 (design system and shell), the RSS wireframe (`docs/design/wireframes/rss.html`).

Decisions marked **(user)** came from the author in the design conversation. Decisions marked
**(builder's call)** were made while writing this down and are open to change in review.

## 1. Intent

The author used to follow several hundred blogs in Google Reader and lost that habit when it
shut down. Today they read Hacker News and a handful of tech blogs, about 30 posts a day, and
want to get back to a much larger volume **(user)**. The reader is a **triage tool**:

1. **Skim** headlines quickly and stop on the few that matter.
2. **Read** a few in depth.
3. **Keep** the ones worth keeping, and later **find them again**.

For a kept post the author wants three things **(user)**:

- a record that it exists, why it was interesting, and how they might use it;
- an archived copy that is **cataloged and searchable**;
- an easy way to come back to it and reference it.

Keeping is usually a **quick note**, sometimes a **longer conversation** with their agent
**(user)**.

**Tags are central (user):** suggested automatically, drawn from one tag catalog shared by the
whole KB, managed in one place, and never duplicated.

### Success

- Several hundred feeds, a few hundred to a few thousand new posts a day, skimmed in one
  sitting with the keyboard.
- Keeping a post with a one-line note takes under ten seconds.
- A kept post is found again through KB search (Notes, ⌘K, MCP `kb_search`) by its words,
  its tags, or the author's note.
- The tag catalog stays clean: no near-duplicate tags appear without the author agreeing.

### Non-goals

- Social features, sharing, comment threads inside the app, recommendations or ranking
  algorithms that reorder the feed.
- Podcasts and video feeds beyond showing their links.
- Syncing read state with other readers (Feedly, NetNewsWire). OPML is the exchange format. *(Reversed for NetNewsWire by the [NetNewsWire sync spec](2026-10-22-netnewswire-sync-design.md).)*
- Full-text search across *unkept* posts beyond the retention window (§4.4).

### Anti-goals (a failure even if it works)

- **Tag sprawl:** a catalog that fills with `sqlite` / `SQLite` / `sql-lite`.
- **Nagging:** unread counts or badges that make a large subscription list feel like debt.
- **Losing the archive:** a kept post whose source disappears and leaves nothing behind.
- **Silent writes:** any KB change (a kept page, a tag merge) the author didn't ask for.

## 2. Reading (the `/rss/` PWA)

### 2.1 Layout (user)

> Amended by the 2026-10-21 feed-management spec (`2026-10-21-rss-feed-management-design.md`).

The classic **three panes** from the wireframe:

- **Feeds sidebar:**
  - views: All unread, In KB;
  - folders, each with an unread count;
  - each feed's own count, and a warning on a feed that keeps failing;
  - **+ Add feed** at the bottom. OPML import and export live in **Settings → RSS** (design
    pass).
- **List:** headlines with a one-line snippet, source and age, newest first, in Desk density.
  Read items dim, and kept items show an "In KB" badge.
- **Reader:** the selected post in the serif reading style (ADR 0008). It shows the feed's own
  content. When that's only a summary, a **Load full article** button fetches and extracts
  the original on demand, and the result is cached in `rss.sqlite` **(builder's call)**:
  - a byline: feed, author, date, reading time;
  - a toolbar on top with **Open original ↗**, **Engage** and **Keep**, always visible at the
    top right (design pass);
  - actions along the bottom: Previous, Next, Mark unread.
- **One way to hold on to a post (design pass):** Keep. There is no separate star or "Save for
  later": the two were redundant.
- **No on-screen key guide (design pass):** the keys work; `?` lists them.

The narrow layout follows the wireframe: list, then reader as a full screen, then the chat as
a sheet.

### 2.2 Keyboard (user: keep it)

| Key | Action |
|---|---|
| `j` / `k` | next / previous post (the reader follows) |
| `Enter` / `o` | open the original in a new tab |
| `n` | **Keep** (quick note, §3.1) |
| `e` | **Engage** (chat, §3.2) |
| `m` | toggle read |
| `Shift+A` | mark everything above the selection as read |
| `g` then `a` / `i` | go to All unread / In KB |
| `/` | search the feed list and headlines |
| `?` | show the key map |

Keys are disabled while a text field has focus **(builder's call)**.

### 2.3 Read state (builder's call)

- **Selecting a post marks it read.** `Shift+A` marks a screenful read. There is no
  mark-on-scroll, because it surprises people at volume.
- **Unread counts are capped in the UI at "999+"**, and folders can be collapsed to hide them
  (anti-goal: nagging).
- Read state and post metadata live in `rss.sqlite`, never in KB files
  (foundation §5.2).

## 3. Keeping a post

Both paths produce the same kind of KB page (§3.3).

### 3.1 Quick: Keep (`n`) (user: usually)

A small popover over the reader:

- **Note:** one or more lines, focused on open.
- **Tags:** chips suggested by the agent (§5.2), editable. A suggested tag that isn't in the
  catalog yet is marked *new*.
- **Keep (⌘↵)** writes the page at once and shows "Kept to Sources/…". **Esc** cancels.
- **Archiving after Keep (builder's call):** the page is written immediately with the
  article text already in hand (a loaded full article, else the feed's content). The full
  archive (extraction and images) then completes in the background through a rev-checked
  `kb.write`.
  - While it runs, the post's list row and the reader show **Archiving…**, then **In KB**. A
    failure shows **Archive incomplete** with the cause and a Retry. That makes the follow-up
    write visible rather than silent.
  - This keeps Keep under ten seconds despite `fetch`'s per-host pacing.

Tag suggestion runs in the background while the popover opens and must not block typing. If
no runner is available, the popover offers tags from simple matching against the catalog
(§5.2) and says so **(builder's call)**.

### 3.2 Longer: Engage (`e`) (user: sometimes)

Agents never keep posts themselves **(builder's call)**. Keeping is always the author's action
(anti-goal: silent writes), so there is no agent-callable keep tool (§6).

- **Opening:** a chat rail opens beside the reader (wireframe "engaged" state), with the post
  as untrusted context (`UntrustedDoc`, fenced). This uses the same agent session machinery
  as Notes (`agents`, a `ContextPack`).
- **Keeping:** **Keep with this conversation** asks the agent, in one more turn, for a
  1–3 sentence takeaway. It then opens the Keep popover with that takeaway shown and
  editable, and the note prefilled from the author's last message **(builder's call)**.
  Whatever the author leaves in the takeaway is written to the page's AI section.
- **Terminal path:** `agents.session_attach` continues an engaged session from Claude Code,
  as in Notes.

### 3.3 The kept page

A kept post becomes `Sources/<Title>.md`. The folder is configurable via `folders.source`,
default `Sources/`, per foundation §5.5. A kept post has `kind: source/rss`:

```markdown
---
id: 01a1…
title: What SQLite's WAL mode actually guarantees
jesinia:
  kind: source/rss
  source: https://fly.io/blog/sqlite-wal
  feed: https://fly.io/blog/feed.xml
  author: Ben Johnson
  published: 2026-10-14
  captured: 2026-10-16T21:40:00-04:00
---
Good explainer on checkpoints; use it when we pick the Jesinia DB settings.

Tags: #sqlite #databases

<!-- jesinia:ai agent=claude-code model=… at=… session=… -->
Takeaway: …
<!-- /jesinia:ai -->

## Article

(the article as Markdown)
```

- **Order:** the author's note comes first, then the tags line, then the AI takeaway (only
  when engaged, with provenance as in foundation §5.3), then `## Article`.
- **The article is the archive (user: "archived … searchable"):**
  - the full readable text, extracted from the original page with a readability pass and
    converted to Markdown;
  - images saved to `_assets/<page-id>/` and linked relatively (foundation §5.3), with a
    per-page cap of 20 images and 10 MB **(builder's call)**. They stay **on this Mac and out
    of git (user)**:
    - each archived page's asset folder gets a `.gitignore` containing `*` **(builder's
      call)**, so KB sync never commits the images while the Wiki and Notes still render
      them through the relative links;
    - the article text itself is in the page and syncs normally;
    - on another machine the page shows the text with broken image links, which is
      acceptable; Open original still works;
  - when extraction fails, the feed's own content is used instead, with a one-line note
    saying so.
- **Keeping again:** keeping the same post again (same `source`) updates the existing page
  instead of making a duplicate:
  - the new note is appended under a dated line;
  - tags are merged;
  - the article is left as first archived.

  Duplicates are found by `source` URL through the KB index **(builder's call)**.
- **Hacker News:** a kept HN item archives the *linked article*. Its front matter also carries
  `discussion: <HN url>` **(builder's call)**.
- Kept pages are ordinary KB pages: the Notes app, the Pigeonpile Wiki, `kb_search` and git
  sync all see them.

## 4. Feeds

### 4.1 Subscribing

> Amended by the 2026-10-21 feed-management spec (`2026-10-21-rss-feed-management-design.md`).

- **Formats:** RSS 2.0, Atom and JSON Feed.
- **Adding a feed:** paste a site or feed URL. Feed autodiscovery finds `<link rel=alternate>`
  on a site.
- **OPML:** import and export in **Settings → RSS** (design pass), alongside retention and the
  kept-posts folder. Import keeps folders; export writes the current list **(user: bring back a
  Google Reader-sized list)**.
- **Folders:** one level, as in Google Reader **(builder's call)**.

### 4.2 Polling (builder's call)

- **Where:** a background job in the server (`core.scheduler`).
- **Fetching:** all requests go through the foundation `fetch` service, so conditional
  requests, per-host rate limits, size caps and SSRF guards come for free.
- **Schedule:** every feed starts at 30 minutes. Each feed then adapts between 15 minutes and
  12 hours, based on how often it publishes. A manual **Refresh** polls everything now.
- **Concurrency:** at most 8 requests at once and at most 1 per host.
- **Failures:** a feed that fails three times in a row shows a warning with the cause, keeps
  being retried with backoff, and is never silently dropped.

### 4.3 Storage

`rss.sqlite`, owned by the `rss` app module:

- `feeds`: the feed URL, title, site, folder, polling state and last error;
- `posts`: GUID, feed, URL, title, author, published date, a snippet, the feed's content, and
  `read`, `saved` and `kept_page_id`.

Post GUIDs are deduplicated per feed, with link + title as the fallback.

### 4.4 Retention (builder's call)

> Amended by the 2026-10-21 feed-management spec (`2026-10-21-rss-feed-management-design.md`).

- **Unkept posts:** pruned 30 days after publication (or after they were first seen, when the feed gives no date); the window is configurable.
- **Kept posts:** never pruned.
- **Search:** the headline search (`/`) covers what's retained. Anything kept is in the KB and
  searchable there forever.

## 5. Tags (KB-wide)

Tags are **KB-wide (user)**, not an RSS feature. They live in the shared `kb` service, so
Notes, RSS, future apps and agents (MCP) all use one catalog.

### 5.1 Storage

- **Pages:** tags stay `#hashtags` in the page body (foundation §5.3). The Pigeonpile Wiki
  already shows them as a tag facet (ADR 0007). Jesinia writes them on a `Tags:` line but
  reads hashtags anywhere in the body.
- **Catalog:** one KB page, `Tags.md` at the KB root, with `jesinia.kind: tag-catalog`
  **(builder's call)**:
  - one line per approved tag, with an optional meaning and its aliases:
    `- #sqlite: SQLite and its internals (aliases: sql-lite, sqlite3)`;
  - because it's a KB page, the catalog syncs with git and travels with the KB, and the
    author can read or edit it in any editor;
  - Jesinia parses it leniently and never rewrites lines it doesn't understand.
- **Index:** the `kb` index tracks tag → pages, for counts and fast lookups.
- **What counts as a tag (builder's call):**
  - the grammar is `#` followed by a letter or digit, then letters, digits or hyphens
    (Unicode letters allowed);
  - it must not be preceded by a word character or `/` (so `page#anchor` and URLs don't
    count), and fenced or inline code and heading lines are ignored;
  - matching is case-insensitive (`#SQLite` = `#sqlite`);
  - Jesinia writes new tags in lowercase, and leaves existing spellings alone except on an
    explicit Rename or Merge.
  - **Verify in the plan:** a fixture page in `docs/design/compat/fixture-kb` checks that the
    Pigeonpile Wiki agrees, for case, hyphens and digits. ADR 0007 didn't record its tag
    rule; a mismatch is recorded there and the grammar narrowed to what both accept.

### 5.2 Suggestions

- **When:** on Keep, and on Save note in Notes.
- **Ranking:** catalog tags first, matched against the text, title and note.
- **Agent:** the author's agent proposes up to five tags, choosing from the catalog when one
  fits. A tag outside the catalog is proposed only when nothing fits, and shows as *new*.
- **Without an agent:** suggestions fall back to catalog matching on words and aliases.

### 5.3 No duplicates (user)

Before a new tag can be created, from any app, MCP or the management screen, it is
normalised and checked against the catalog:

- **Normalisation:** lowercase, hyphens for spaces, and Unicode NFC.
- **Exact and alias matches:** an existing tag or alias is used directly.
- **Near-matches:** plural/singular, edit distance ≤ 2 on tags of 5+ letters, and the same
  word with different separators. The near-match is offered *instead*: "Use #sqlite?".
- **Meaning matches:** when a runner is available, the agent is asked whether the new tag
  means the same as an existing one (`ml` / `machine-learning`), and the answer is offered as
  a suggestion. The author decides; nothing is merged silently.

A new tag joins the catalog only when the author accepts it. A tag an agent writes through MCP
`kb_write` that isn't in the catalog is reported as *uncataloged* (§5.4), not added
automatically.

### 5.4 Tag management (Settings → Tags)

The screen lives in Settings, because tags are KB-wide **(builder's call)**:

- **Catalog list:** every tag with its page count, meaning and aliases. You can search,
  sort by name or count, and filter.
- **Rename:** rewrites `#old` to `#new` in every page that uses it, and adds `old` as an
  alias.
- **Merge:** pick two or more tags into one; aliases are kept.
- **Retire:** keeps the tag in pages but hides it from suggestions. A retired tag can be restored (design pass).
- **Uncataloged tags:** hashtags found in pages but not in the catalog (hand edits, the Wiki,
  agents), each with **Adopt**, **Merge into…** or **Ignore**.

Rename and merge are KB writes. Pages in reserved folders (`Journal/`, `Drafts/`), which
Jesinia never writes, are left unchanged. The preview lists them as **not updated**, and the
old name stays as an alias so those pages still resolve **(builder's call)**:

- they preview the affected pages before applying;
- they go through `kb.write` with base revisions;
- the git sync commits them as one change **(builder's call)**.

### 5.5 MCP tools

These are the same rules for agents:

- `tags_list`: the catalog with counts.
- `tags_suggest(text)`: ranked suggestions, flagging *new*.
- `tags_check(tag)`: normalisation and near-matches.

Tag *creation* stays a user action. A hashtag an agent writes through `kb_write` that isn't
in the catalog shows up as *uncataloged* (§5.4).

## 6. Architecture

- **The `rss` app module** (`server/jesinia/apps/rss/`), layered
  domain ← application ← infrastructure ← api like Notes:
  - feeds, polling, posts and keep orchestration;
  - REST under `/api/rss/`;
  - read-only MCP tools `rss_search_posts` and `rss_get_post`. There is no keep tool:
    keeping is a user action (§3.2).
- **Tags** extend the shared `kb` service (`services/kb/`): the catalog parser, the index,
  suggestions and checks. REST lives under `/api/kb/tags`, plus the MCP tools in §5.5.
- **Article extraction** is a new infrastructure helper in `fetch`: readability to HTML to
  Markdown, sanitised with the existing allowlist. The library choice is the plan's, under
  the dependency rules (license-compatible with AGPL, pinned).
- **Layout patterns from the design pass** (`docs/design/wireframes/design-pass-2026-10-16.md`):
  Settings uses an outline of sections on the left and a view pane with a toolbar on top and
  actions at the bottom; Settings gains an **RSS** section (OPML, retention, kept-posts folder).
- **The PWA** is `web/apps/rss/`, from the Notes template (`AppShell`, `data-app="rss"`,
  manifest, service worker). It uses the RSS icons already exported in
  `docs/design/icons/rss/export/`; RSS is added to `SHIPPED` and set `built` in `apps.ts`.
- **Settings → Tags** lives in the launcher app.

## 7. Errors (foundation §13 applies)

> Amended by the 2026-10-21 feed-management spec (`2026-10-21-rss-feed-management-design.md`).

- **A feed fails:** a warning on the feed, with its cause and a Retry. The rest keep polling.
- **Extraction fails on Keep:** the page is still written, with the feed content and a note.
  Keep never fails because the article couldn't be extracted.
- **Keep while the KB is unavailable:** the action is refused with the KB banner's message,
  and nothing is queued silently.
- **Tag rename or merge conflicts with an edited page:** that page is skipped and listed. The
  rest apply.

## 8. Testing

- **Python:** 95% per file (domain 100%). Fixture feeds (RSS, Atom, JSON Feed, broken XML),
  OPML round-trips, polling backoff with a fake clock, keep idempotency, and the tag catalog
  parser, normalisation and near-match table.
- **Web:** vitest for the keyboard map, the list, the Keep popover and Settings → Tags.
- **e2e:** Playwright flows:
  - add a fixture feed from the e2e fixture server, skim with `j`/`k`, keep with a note and
    a suggested tag, then find the page in Notes search;
  - rename a tag and see every page update.

## 9. Left to the plan

- Libraries for feed parsing, readability, and HTML→Markdown.
- Exact SQLite schema and indexes.
- The polling backoff formula.
- Whether Settings → Tags ships in the same PR as the tag service or the one after.
- PR breakdown and order. The tag service first is likely, since RSS Keep depends on it.

## 10. Acceptance (2026-10-17)

Run on the author's Mac against the auto-updating install (`~/apps/jesinia`, port 5374),
driven by Claude in the browser with the author's approval. Shipped in #28 (tag catalog),
#29 (backend), #35 (app), #37, #38, #39 and #41 (fixes found during acceptance).

| Step | Result |
|---|---|
| 1. Add feeds | Hacker News added by site URL (discovery found the feed; 30 posts, folder *Tech*); Simon Willison and Julia Evans added into *Blogs*. |
| 2. Keep quickly | `n`, a note, `⌘↵` wrote `Sources/Replacing the old battery on rechargeable bike lights.md`; the status moved from *Archiving…* to *In KB* live, through `rss.post_changed`. |
| 3. Keep through Engage | A read-only `claude-code` session answered a question; *Keep with this conversation* prefilled the note and an editable takeaway; the page got the chosen tags, the `jesinia:ai` provenance block and the takeaway, and `Tags.md` was created. |
| 4. Images stay local | 4 images in `_assets/<id>/` with a `.gitignore` of `*`; KB sync committed and pushed only the two pages and `Tags.md` (`git ls-files` lists no `_assets/`). |
| 5. Rendering | Fixed during acceptance: clicking a suggested tag lost focus (#37); archived articles turned anchor-link headings into code blocks and kept site clutter (#38); Notes had no rendered view and never showed local images (#39); Notes squeezed the page on narrow windows (#41). After the fixes the two pages were rebuilt, and both render in Notes' Read view with clean headings, local images, tag chips and the AI callout. |
| Pigeonpile Wiki | Not checked yet: it waits for the next Pigeonpile Wiki release (#53). ADR 0007 row h stays *pending manual check*. |

Follow-ups: #30, #31, #32 (closed by #43), #36.

