# Jesinia — RSS Feed Management, Bankruptcy and Auto Full Article

- **Status:** Implemented
- **Acceptance:** 2026-10-21, branch `feat/rss-feed-management`: all nine plan tasks shipped; unit tests, the e2e suite
  (including the done-for-the-day path and a reload at `/rss/manage`) and `make ci` pass.
- **Date:** 2026-10-21
- **Author:** Don Albrecht (with Claude)
- **Builds on:** the RSS reader spec (`2026-10-16-rss-reader-design.md`, Implemented). This
  amends §2.1, §2.3, §4.1, §4.2, §4.4 and §7 there; where the two disagree, this one wins.
- **Mockups (throwaway):** `.superpowers/brainstorm/26841-1792582447/content/` —
  `reader-layout.html`, `manage-feeds-v2.html` (git-ignored; described in full below).

Decisions marked **(user)** came from the author in the design conversation. Decisions marked
**(builder's call)** were made while writing this down and are open to change in review.

## 1. Intent

Three frictions in daily reading:

1. **The pile.** The author's routine is two passes: first clear a few specific blogs they
   won't read, then declare the day done — sometimes per folder, sometimes the whole reader.
   Today that's only possible post by post, or with `Shift+A` a screenful at a time.
2. **Feed upkeep leaks into reading.** Failing-feed warnings and Retry buttons sit in the
   sidebar, and the only way into feed management is a small dialog behind **+ Add feed**.
   Sync health is an upkeep task; it should live on its own screen, out of the reading view.
3. **Summaries.** Many feeds carry only a summary. Today the full article loads only when you
   press **Load full article**, and that button only appears when the text looks truncated.

### Success

- Clearing a blog takes one hover-and-click in the sidebar; ending the day takes one click
  at whatever level is showing.
- The reading view shows no sync errors at all.
- Opening a post shows the full article without asking, whenever extraction can get it.
- Every feed's health, retention and folder are managed from one screen.

### Non-goals

- Per-post or per-feed scheduling controls (poll interval stays adaptive, §4.2 of the RSS
  spec).
- Nested folders (still one level).
- Changing Keep / Engage / the kept page.

### Anti-goals

- **Marking posts read that the user never had a chance to see** (ones that arrived mid-click).
- **Nagging:** confirm dialogs on a daily routine, or error badges in the reading view.
- **Losing a broken feed silently** — removing it from the sidebar's attention must not mean
  nobody can find out it's broken.

## 2. Mark read in bulk ("bankruptcy")

### 2.1 Where (user)

- **Middle bar (primary):** the list pane header gets **✓ Mark all read**. It applies to the
  **current aggregation level**: the selected feed, the selected folder, or **All unread**
  (everything).
- **Sidebar (secondary):** hovering a feed row or a folder row swaps its unread count for a
  small **Mark read** control that marks that feed / folder read without selecting it.
  Hover-only controls need a keyboard path **(builder's call):** the control is also reachable
  by focus (it shows on `:focus-within` of the row); keys are in §2.3.

### 2.2 Behaviour

- **Keep is unaffected (user).** Mark read only changes read state; kept posts stay kept and
  stay in In KB.
- **Only what was seen (builder's call).** The request carries an as-of time (the newest
  `first_seen` the client has for that scope, or the time the list/counts were loaded); posts
  first seen after it stay unread. This mirrors how `read-above` already takes `as_of`.
- **No confirm dialog; Undo instead (builder's call).** A toast says "Marked 187 in Tech read"
  with **Undo**. Undo restores exactly the posts that call changed (the server returns their
  ids, or the client keeps an undo token — builder's choice), for as long as the toast shows.
- In the **In KB** view the middle-bar button is hidden (that view is about kept posts, not
  unread ones).
- While a search is active the button is hidden **(builder's call)**: "mark all read" over a
  search result is a different, riskier feature.
- After marking, counts update, the list reloads to the empty-state for that scope, and the
  reader pane clears if its post was in scope.

### 2.3 Keyboard (builder's call)

- `Shift+A` keeps its meaning (mark everything above the selection read).
- New: `Shift+M` — mark all read at the current level (same as the middle-bar button).
- The sidebar hover control is a real `<button>` inside the row, so Tab reaches it.

### 2.4 Server

One endpoint, e.g. `POST /api/rss/posts/mark-read` with
`{scope: {feed?: id, folder?: id} | "all", as_of}` → `{marked, undo: [...ids] | token}`; idempotent
like the other mutating RSS routes. The exact shape is the builder's.

## 3. Refresh

- **↻ Refresh moves from the title bar to the list pane header (user)**, next to Mark all
  read. It refreshes all feeds, as today, and keeps the "Updated 3m ago" text beside it
  **(builder's call)**.
- The title bar no longer carries Refresh.

## 4. Errors leave the reading view (user)

- The sidebar no longer shows the `!` warning or the inline cause + Retry (`FeedProblem`).
- Feed failures appear only on the management screen (§5).
- **Discoverability (builder's call):** the sidebar's **⚙ Manage feeds** entry shows a quiet
  count when feeds are failing ("⚙ Manage feeds · 3"), no colour beyond the muted text tone.
  Without this, a dead feed could go unnoticed indefinitely (anti-goal 3). Drop it if it
  reads as nagging.
- Errors that are about the *reading* itself (couldn't load posts, couldn't mark read) still
  show where they happen, as today.

## 5. Feed management screen

### 5.1 Entry and shape

- **+ Add feed** at the bottom of the sidebar becomes **⚙ Manage feeds** (user).
- It replaces the existing modal with a **full screen** that takes over the reader area
  (list + reader panes), with **‹ Back to reading** to return **(builder's call)**. The URL
  reflects it (e.g. `/rss/manage`) so it can be bookmarked and Back works.
- Narrow layout: same screen, rows stack (feed name on top, controls below).

### 5.2 Header

- **Add a feed (user):** a URL field (site or feed — autodiscovery as today), a folder picker
  (existing folders, or type a new one), **+ Add feed**.
- **Sync all** — polls every feed now (the same action as Refresh).
- **Import OPML** and **Export OPML (user).** They also stay in Settings → RSS **(builder's
  call)**; both places call the same endpoints.
- **Default retention** for feeds without their own setting (the existing global window).
- A filter: **All (42) · Failing (3)**.

### 5.3 One row per feed (user unless marked)

| Column | Content |
|---|---|
| Feed | title; the feed URL beneath it, muted |
| Folder | a picker; changing it **moves** the feed. Renaming a folder is offered here too **(builder's call)** |
| Last synced | relative time of the last *successful* poll (`last_ok_at`), absolute on hover |
| Newest post | how long ago the newest post was published ("3 weeks ago") — shows a feed that still syncs but has gone quiet (user) |
| Status | **OK**, **Syncing**, or **Failing (n tries)** with the cause in plain words beneath |
| Keep posts | per-feed retention (§6) |
| Actions | **Sync now** (polls this feed; the row updates in place), **Remove** (unsubscribe) |

- **Remove** follows today's unsubscribe semantics (kept posts stay in the KB). It uses an
  Undo toast rather than a confirm dialog: the row hides at once and the unsubscribe is sent
  when the toast closes without Undo **(builder's call)**.
- Sort: alphabetical by title, failing feeds first when the Failing filter is off
  **(builder's call)**.
- A feed that's quiet but syncing fine is **not** flagged; the Newest post column is the
  signal **(builder's call)**.

### 5.4 Server

`FeedOut` grows the fields the table needs: `last_ok_at`, `fail_count`, `last_error` (the
existing `warning` may stay as the summarised cause), `newest_post_at`, `retention`, and a
syncing flag if one is cheaply available (otherwise "Syncing" is purely client-side while a
Sync now / Sync all call is in flight — builder's choice).

## 6. Per-feed retention (user)

"30 days of Jason Fried and 30 days of Hacker News are very different."

- Each feed has an optional retention override; without one it uses the default (today's
  global `rss.retention`, 30 days).
- **Unit (user):** each feed chooses **days** (keep posts newer than N days) or **newest N
  posts**. The default for feeds without an override stays the global days window (30 days),
  days-only.
- **An N-post cap only removes read posts (user).** Unread posts beyond the cap are kept until
  read (bankruptcy, §2, is how they get cleared), so a busy feed never deletes something the
  user hasn't seen. A days window prunes as today (read or unread).
- Kept posts are never pruned, under either unit (unchanged).
- Stored in `rss.sqlite` on the `feeds` row (it's feed state, not a setting), migrated as
  nullable columns (a kind and a value); null means the default.

## 7. Full article loads automatically (user)

- Opening a post **automatically attempts** the full-article extraction (the existing
  fetch + extractor + `rss.sqlite` cache). There is no button press.
- It fires for **every** post opened, not only ones that look truncated **(builder's call)**:
  short summaries are exactly the case it's for, and the cache makes repeat opens free.
- **Display:** the feed's own content shows immediately; when extraction succeeds, the reader
  swaps to the extracted article. A subtle "Showing full article · Show feed text" toggle
  lets the user go back when extraction picks the wrong thing **(builder's call)**.
- **Failure is quiet (builder's call):** no error banner; the feed text stays, with the usual
  **Open original ↗**.
- **Rapid `j`/`k`:** only the selected post's extraction matters; switching posts abandons the
  previous request's result (abort or ignore — builder's choice). Prefetching neighbours is
  out of scope.
- **Keep** already prefers a loaded full article over the feed text (RSS spec §3.1); that
  stays, and now applies more often.

## 8. Testing

- Server: bulk mark-read per scope (feed, folder, all) with `as_of` excluding newer posts;
  kept posts' `kept_page_id` untouched; undo restores exactly the marked set; per-feed
  retention prune (days, and newest-N removing only read posts) next to the default; `FeedOut` new fields.
- Web: middle-bar button follows scope; sidebar hover/focus control; no warnings in the
  sidebar; management screen add / move / remove / sync now / OPML / retention; reader
  auto-loads and swaps, and stays quiet on failure; stale extraction ignored on fast
  navigation.
- E2E (`web/tests/e2e/rss.spec.ts`): the "done for the day" path — clear one feed from the
  sidebar, then Mark all read at All unread, then Undo.

## 9. Left to the builder

Endpoint shapes, undo mechanism, how "Syncing" is detected, component split of
`ManageFeeds.tsx`, CSS, and copy beyond what's quoted here.
