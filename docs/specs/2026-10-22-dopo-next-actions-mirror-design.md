# Jesinia — DOPO Tasks Next-Actions Mirror

- **Status:** Proposed
- **Date:** 2026-10-22
- **Author:** Don Albrecht (with Claude)
- **Builds on:** the foundation spec (`2026-10-16-jesinia-foundation-design.md`), ADR 0003
  (format-only Pigeonpile interop), ADR 0004 (files are the source of truth), the
  [KB format](../conventions/kb-format.md) and [architecture](../conventions/architecture.md)
  conventions.
- **Plan:** `docs/superpowers/plans/2026-10-22-dopo-next-actions-mirror.md`

Decisions marked **(user)** came from the author. Decisions marked **(builder's call)** were
made while writing this and are open to change in review.

## 1. Intent

The KB runs GTD-flavored PARA. Next actions live in **DOPO Tasks** (the GTD app in the
Pigeonpile suite) **(user)**. They should also show up in the KB. Each active project page
(`~/kb/10 Projects/<Project>/<Project>.md`) has a `## Next Actions` section that currently
reads "Mirroring into this page is not set up yet." `01 Today/Today.md` should list today's
actions too.

DOPO Tasks has no API, CLI or MCP server. Its only agent-facing interop is a canonical
**`tasks.md`** file in its sync folder, which DOPO reconciles two-way with its database
(§3). Jesinia already shares the KB with the Pigeonpile Wiki **through file formats only**
(ADR 0003). This mirror follows the same rule: Jesinia reads and edits `tasks.md`. It never
imports suite code and never talks to DOPO over a network.

### Success

- Opening a project page in Notes (or the Wiki, or any editor) shows that project's current
  next actions, with context, due date and waiting-for, at most a minute behind DOPO.
- `01 Today` shows what DOPO would put on Today, grouped by project, linked to project pages.
- Ticking an action in the KB marks it done in DOPO the next time DOPO is open (Phase 2).
- Nothing the user types in a KB page is ever lost to the mirror.

### Non-goals

- Creating, renaming, re-dating or re-filing actions from the KB. DOPO is where actions are
  edited. The KB shows them and can only tick them off.
- Mirroring inbox, someday, done or trashed items, or actions that have no project.
- Mirroring DOPO projects or areas *into* the KB as new pages. Project pages are made by
  hand (or by an agent) and then matched (§4).
- A new PWA. The mirror has no UI beyond a Settings card (§7).

### Anti-goals

- **Clobbering user text.** The mirror owns one marked region per page and nothing else.
- **Git churn.** A sync with nothing new must not rewrite a page (no timestamps in the region).
- **Silent breakage.** A missing sync folder, an unmatched project, or a tick that never
  landed shows up in Settings and `/healthz`. It does not just quietly stop.
- **Fighting DOPO.** Jesinia never rewrites the whole `tasks.md` and never mints ids or revs.

## 2. Decisions at a glance

| Question | Decision |
|---|---|
| Read-only or two-way? | **Phased (builder's call).** Phase 1: read-only mirror plus Today. Phase 2: two-way, *completion only*. A tick in the KB edits that one line of `tasks.md`, and DOPO ingests it with its existing 3-way reconcile. |
| How DOPO projects map to KB pages | **By name (builder's call).** A page under `10 Projects/` matches the DOPO project whose name equals the page title (NFC, case-insensitive). A `jesinia: {tasks_project: "<name>"}` front-matter key overrides it. Ambiguity and misses are reported, never guessed (§4). |
| Where the mirror runs | **A new Jesinia service, `tasksmirror` (builder's call)**, in the one server process. It polls the configured `tasks.md` by mtime and writes pages through `KbService` with `expected_rev` (§5). |
| How `01 Today` gets today's actions | **The same service renders a region in `01 Today/Today.md`** using DOPO's own Today rule, as far as `tasks.md` exposes it: due by end of tomorrow, or defer date reached (§6). |

## 3. The `tasks.md` contract (as observed)

The source of truth for the dialect is the suite. Jesinia reimplements a reader for it
(ADR 0003 forbids importing the proprietary codec) and pins it with fixtures (§9). These are
the facts the design depends on, read from `crates/amicus-records/src/tasks_md.rs`,
`apps/tasks/src/domain/tasks-md.ts` and `apps/tasks/src/application/services/tasks-md-sync.ts`
in `amicus-platform`:

- **Location.** One `tasks.md` per DOPO sync folder, beside the `.pile` replicas. The user
  connects the folder in DOPO (File System Access). Jesinia is told the same folder's path.
- **Lines.** Three kinds: task lines, `##`+ section headings, and passthrough lines kept
  verbatim. A task line is
  `- [ ] Title @status:<state> @context:<name> @project:<name> @energy:<lvl> @waiting:"…" due:YYYY-MM-DD defer:YYYY-MM-DD <!-- id:<itemId> rev:<rev> -->`.
  Tokens are order-independent on read. Unknown tokens round-trip.
- **States.** `inbox next in-progress blocked waiting someday done trash`. The checkbox is
  derived from the state: `done`/`trash` ⇒ `[x]`. **An explicit `@status:` wins over the
  checkbox.** If the line already carries `@status:next`, ticking the box does nothing.
- **Who writes it.** DOPO rewrites the whole file from its database after every in-app change
  (800 ms debounce). It keeps sections and prose, and appends new items at the end. Items
  are ordered oldest first. Every live item is in the file, done ones included. Only
  trashed and deleted items are left out.
- **External edits.** DOPO's leader tab polls the file's mtime every 2 s *while the app is
  open*. A changed line whose trailer `rev` still matches the database is ingested (`ff-db`).
  If both sides changed, the line becomes a conflict for DOPO's resolver dialog and nothing
  is applied. An id-less line becomes a new item.
- **When DOPO is closed** nothing reconciles. The file is only as fresh as the last time DOPO
  ran. An edit made while it is closed is ingested when it next opens.
- **What is missing.** Areas, project ids, manual order, priority, and DOPO's "hand-added to
  Today" marker are not in the file. Project and context appear only as display names.

## 4. Project mapping

### 4.1 Rule (builder's call)

1. Jesinia considers **project pages**: the folder note `<X>/<X>.md` of a project folder in
   `10 Projects/`, `10 Projects/_Paused/` or `10 Projects/_Someday/` (for example
   `10 Projects/Mote3d/Mote3d.md`, `10 Projects/_Paused/SCAD on Mac/SCAD on Mac.md`). Pages
   under `40 Archive/` and `20 Areas/` are not project pages. A former project that became
   an area (MakerTools) stops being mirrored when its folder moves.
2. A page's **project key** is `jesinia.tasks_project` from its front matter if present,
   otherwise its title.
3. A DOPO project name **matches** a page when NFC-normalized, case-folded and trimmed
   strings are equal.
4. Each action line goes to the page its `@project:` matches. Lines with no `@project:` are
   not mirrored (single actions live in DOPO only).

Matching by name fits how the pages were seeded: each placeholder already names its DOPO
project, and each title is a DOPO project name. An explicit key handles a rename on either
side without renaming the other.

### 4.3 Project status (builder's call)

The KB states a project's status by folder: active, `_Paused/` or `_Someday/`. DOPO has its
own `ProjectStatus`: `active`, `someday` or `done`. There is no *paused*, so a KB-paused
project is presumably `someday` or `active` in DOPO. **`tasks.md` carries no project status
at all**, so Jesinia can neither read nor set it. The design follows from that:

- **Every project page is mirrored, whatever its folder.** A paused project's open actions
  (often a *waiting* item) stay visible on its page. The region looks the same everywhere.
- **Moving a folder is the KB's statement only.** Jesinia doesn't tell DOPO. The user changes
  the project's status in DOPO too.
- **Drift is flagged where it can be seen.** A page under `_Paused/` or `_Someday/` with
  `next` or `in-progress` actions is listed in Settings as *paused with next actions*, since
  in GTD a paused project shouldn't have next actions on the user's lists. The opposite
  check (a DOPO project that is `someday` but sits in the active folder) needs project
  status in the dialect, which is a follow-up ask (§10).
- **Today ignores the folder.** A dated action shows on Today whatever its project's status,
  as DOPO would show it.

### 4.2 Misses and collisions

| Case | Behavior |
|---|---|
| DOPO project with open actions, no matching page | Not written anywhere. Listed in Settings as *unmatched* with its action count. |
| Project page with no DOPO project | Its region says *No next actions in DOPO Tasks.* (an empty project is a GTD smell worth seeing). |
| Two pages claim the same project | Neither is written. Both listed as a *collision* until one sets `tasks_project`. |
| Page has no `## Next Actions` heading | Skipped, listed as *no section*. The mirror never adds headings to user pages. |

A rename in DOPO therefore shows up as one *unmatched* project plus one empty page, which
the user fixes with a title change or `tasks_project`.

## 5. The mirror service

### 5.1 Placement (builder's call)

`server/jesinia/services/tasksmirror/`, a `Module` like `kbsync`. It is a service rather than
an app because it has no PWA and other modules could use it later (an agent tool, a Today
view). It depends on the `kb` service through `ctx.services` only.

- `domain/`: the `tasks.md` reader and the line editor (§8.2), the action filter, the
  page-region renderer, the Today rule. Pure, 100% coverage.
- `application/`: `MirrorService` (one pass: read, map, render, write) and ports for the
  file source and a small ledger (§8.3).
- `infrastructure/`: the filesystem `tasks.md` source (read, mtime, atomic line edit) and the
  SQLite ledger.
- `api/`: `GET /api/tasksmirror/status` and `POST /api/tasksmirror/sync` for Settings. No MCP
  tools in v1. Agents already see the mirrored text through `kb_read`.

### 5.2 Config

A new `tasks_md_path` key (the full path to DOPO's `tasks.md`, unset by default) is set in
Settings or with `jesinia config set tasks_md_path …`. Settings saves it through
`PUT /api/tasksmirror/path`, which validates the path, persists it, updates the running
config, and rebinds the module at once, the way `PUT /api/kb/path` rebinds the KB. No
restart is needed. While it is unset the module is
healthy and idle, and Settings shows "Not connected". A configured path that doesn't exist
makes the module report *degraded*, with the path in the message.

### 5.3 Triggers

- **Poll.** Every 15 s the scheduler checks the file's mtime and size. On a change it runs a
  pass. (Polling, like DOPO's own leader, avoids depending on FS events that cloud-synced
  folders deliver unreliably.)
- **KB edits (Phase 2).** `kb.page_changed` for a mapped page, with an origin other than this
  module, adds the page id to an in-memory *dirty pages* set and starts
  `scheduler.run_now("tasksmirror")` as a background task (`run_now` awaits the whole job, and
  the KB write path must never wait on the mirror). The handler does no other work. The job drains the
  set and runs the completion check (§8) for those pages before its pass.
- **Midnight.** A pass at local midnight re-renders Today even if `tasks.md` hasn't changed.
- **Manual.** Settings → **Sync now**.

All work runs inside the one scheduled job, so the scheduler lock serializes it: a poll never
overlaps a KB-triggered check. `EventBus.publish` awaits handlers directly, outside that lock,
which is why the handler only marks pages dirty and never touches the ledger, the file or
pages itself.

### 5.4 One pass

1. Read and parse `tasks.md`. If it can't be read, stop: no page is touched and the error is
   recorded with its cause.
2. Select **open actions**: state `next`, `in-progress`, `waiting` or `blocked`, with no
   `defer:` date after today.
3. Group by mapped page (§4) and render each page's region (§5.5) and the Today region (§6).
4. For each page whose rendered region differs from what is on disk, call
   `KbService.update_body(id, body, expected_rev=rev, origin="tasksmirror")`. `update_body`
   currently always emits `kb.page_changed` with origin `jesinia`, so it gains an `origin`
   keyword (default unchanged). The Phase 2 tick detector skips events with this origin,
   so the mirror's own writes (including a rendered `- [x] … · pending DOPO` line) are never
   read as user ticks. On `StaleWrite`, re-read and retry
   once, then leave it for the next pass.
5. Record a per-page outcome for Settings.

The mirror is a secondary subsystem. A failure is logged with the original cause, counted in
status, and never raised into the KB write path or another module.

### 5.5 The region

The mirror owns exactly the lines between two markers placed directly under
`## Next Actions`. Everything above and below is the user's.

```markdown
## Next Actions
<!-- jesinia:mirror source=dopo-tasks project="Jesinia" -->
- [ ] Draft the tasks mirror spec · @computer · due 2026-10-24 <!-- dopo:01a1…@9f2c… -->
- [ ] Ask Sam about the review cadence · waiting: "Sam" <!-- dopo:01a2…@41be… -->
<!-- /jesinia:mirror -->
```

- **Line shape.** `- [ ] <title>` then ` · @<context>`, ` · due <date>`, ` · waiting: "<reason>"`
  when present, then a `<!-- dopo:<itemId>@<rev> -->` trailer. It carries the id and the
  `tasks.md` rev the line was rendered from, both for Phase 2. The rev changes only when the
  item's content changes in DOPO, so it adds no churn beyond what the content already
  causes. (A change to a field the region doesn't show, such as energy, rewrites the
  trailer. That is rare and accepted.)
  `in-progress` items get a leading `▸ `, and `blocked` gets ` · blocked`.
- **Order.** Dated items first by due date, then the rest in `tasks.md` order (DOPO's
  creation order).
- **Empty.** `_No next actions in DOPO Tasks._`
- **No timestamp** in the marker, so an unchanged project never rewrites its page. Freshness
  ("last synced 12 s ago") lives in Settings.
- **First run.** When the section has no markers yet, the mirror replaces the section's
  contents up to the next heading **only if** they equal the known seeded placeholder
  ("Live in DOPO Tasks (project …). Mirroring into this page is not set up yet."). Otherwise
  it inserts the region at the top of the section and leaves the existing text below it.
- **Text the user adds inside the region.** Lines that aren't rendered task lines are
  moved, verbatim, to just below the closing marker on the next pass. They are never
  deleted. Edits to a rendered line's text are overwritten (DOPO is where actions are
  edited), but the tick itself is honored in Phase 2.
- **Titles** are rendered as text. `<!--` and `-->` inside a title are neutralized the same
  way `notes_save` neutralizes them, so a task title can't forge a marker.

The markers are a new KB format element: `jesinia:mirror` joins `jesinia:ai` in
`kb-format.md`. The Wiki already preserves Jesinia's metadata and hides HTML comments
(ADR 0007, checks a and c). The plan re-runs the compat fixture with a mirror region before shipping.

The project pages sit inside an existing `<!-- jesinia:ai … -->` provenance block. That
block is advisory. The mirror region may sit inside it, and the mirror leaves the
provenance markers alone.

## 6. `01 Today`

### 6.1 Rule (builder's call)

DOPO's Today agenda is: live and not done, and any of: due before the end of tomorrow,
defer date reached, hand-added to Today today, or has a priority rank. `tasks.md` carries
only the first two, so Jesinia's Today is:

> an action in state `next`, `in-progress`, `waiting`, `blocked` **or `inbox`**, that is
> **due on or before tomorrow** (including overdue) **or whose `defer:` date is today or
> earlier**.

Unlike project pages, Today includes actions with no project and inbox items that have a
date, because a dated item belongs on Today even if it hasn't been triaged.

Hand-added and prioritized items are missing until the dialect carries them. This needs a
follow-up issue on `amicus-platform` asking for `@today:<date>` and `@priority:<n>` tokens.
When they appear, the reader keeps them in `extra_tokens` today and only the rule changes.

**Days are local (builder's call).** DOPO computes "today" in UTC (`startOfUtcDayTs`), so
in the evening (US time zones) DOPO's Today rolls over hours before Jesinia's. Jesinia uses
the Mac's local date, which is what the user means by today. The difference is documented,
not copied.

### 6.2 Region

Under a `## From DOPO Tasks` heading at the top of `Today.md` (created once, on the first
pass, after the page's intro paragraph; the only heading the mirror ever creates):

```markdown
## From DOPO Tasks
<!-- jesinia:mirror source=dopo-tasks view=today -->
**Overdue**
- [ ] Renew passport · due 2026-10-20 · [[01a1639a-0a3e-…|Paris Trip]] <!-- dopo:…@… -->

**Today**
- [ ] Order seed catalog · [[01a1639a-0a9a-…|2027 Garden Planning]] <!-- dopo:…@… -->

**Tomorrow**
- [ ] Book Rhine cabin · due 2026-10-23 · [[01a1639a-0a5d-…|Rhine Trip]] <!-- dopo:…@… -->
<!-- /jesinia:mirror -->
```

Groups: Overdue, Today (due today or defer reached), Tomorrow. Empty groups are left out.
With nothing at all, the region reads `_Nothing from DOPO Tasks today._`.
A project link uses the page id, so it survives moves. An unmatched project shows as plain
text `(Project name)`.

The page's own instruction ("Clear it at the end of the day") applies to the user's items.
The mirror region refreshes itself, at midnight and on every change.

## 7. Status and Settings

`GET /api/tasksmirror/status` returns: `connected`, `path`, `last_read_at`, `last_error`
(cause + what to do), `pages` (id, title, project, action count, outcome), `unmatched`
(project name, action count), `collisions`, `pending_completions` (Phase 2).

Settings gets a **DOPO Tasks** card: the path field, "Last synced …", **Sync now**, then
lists of unmatched projects, collisions, pages without a section, paused projects with
next actions (§4.3), and ticks still waiting for DOPO. `/healthz` reports the module's health like any other.

## 8. Phase 2: ticking off from the KB

### 8.1 Detecting a tick

On a `kb.page_changed` for a mapped page (or Today) from another origin, the service reads
the page and parses its region. A rendered line `- [x] … <!-- dopo:<id>@<rev> -->` whose
item is open in `tasks.md` is a **completion request**, and its **base rev is the rendered
rev from the KB trailer**, not whatever `tasks.md` holds now. If the item's current `tasks.md`
rev differs, the user ticked a version that DOPO has since changed. The request is recorded
as *superseded* straight away and nothing is written. The next render shows the current
version, which the user can tick again. Unticking is ignored (re-opening happens in
DOPO). On the next render the ticked line drops out of the region once the edit is applied.

### 8.2 Writing the completion

Jesinia edits **only that line** of `tasks.md`. It reads the file, finds the line by its
trailer id, changes it, and replaces the file atomically. The checks:

- The box becomes `[x]`, and the `@status:` token is set to `@status:done` (added or
  replaced). Setting only the box would not work, because an explicit `@status:` wins (§3).
- **The trailer `<!-- id:… rev:… -->` is left exactly as it was.** DOPO detects the edit
  because the content no longer hashes to `rev`. If its database is still at `rev` it
  ingests the edit as `ff-db`. If the item also changed in DOPO, its resolver shows both
  sides. Jesinia never computes revs.
- Every other byte of the file is unchanged.
- **Installing the edit never discards a DOPO write.** A stat check before a plain rename
  isn't a compare-and-swap: DOPO could install a newer file between the check and the
  rename, and the rename would throw it away. Jesinia uses an atomic **swap** instead:
  `renamex_np(tmp, tasks.md, RENAME_SWAP)` on APFS. After the swap, `tmp` holds exactly the
  file that was in place at the moment of the swap. Jesinia compares it with the text it
  edited:
  - **Same:** done. The displaced copy is deleted.
  - **Different** (DOPO wrote in between): DOPO's newer text is in hand, not lost. Jesinia
    re-applies its one-line edit to it (or makes no edit if that line is now done, gone, or
    has a new `rev`) and swaps again. It never installs a snapshot it hasn't just been
    handed by a swap, and there is no final unguarded "restore" step.
  - **Bounded, with recovery.** After 5 rounds that each found a newer file, Jesinia stops
    swapping. The file in place is then its edit of DOPO's *previous* render, and the latest
    displaced copy is saved verbatim to
    `<data_dir>/tasksmirror/displaced/<UTC timestamp>.md`. Settings shows "DOPO Tasks
    rewrote tasks.md during a write; its last version was saved to …", and the request stays
    pending. Nothing is silently lost. For DOPO's own writes this also heals on its own: each
    DOPO write renders its database, and the older lines Jesinia left in place still carry
    their older revs, so DOPO's next pass (triggered by the mtime bump) classifies them as a
    stale file (`ff-disk`) and re-renders from the database. The saved copy matters only for
    a third writer, such as an agent editing `tasks.md`. DOPO debounces its writes by 800 ms,
    and a swap round takes milliseconds, so reaching 5 rounds means something is wrong.
  - **Swap unsupported** (not APFS, or the call fails): Phase 2 writes are disabled and
    Settings says why (cause plus "Move the DOPO sync folder to an APFS volume"). It never
    falls back to a plain rename.
  DOPO's own writes go through File System Access, which writes a swap file and renames it
  into place, so every DOPO write is a whole-file replacement that the swap handles.

### 8.3 The ledger

DOPO rewrites the whole file from its database, so a DOPO pass that read the file just
before Jesinia's edit can overwrite it. Each request is therefore kept in a small SQLite
ledger (`tasksmirror.db`, class *history*): item id, **base rev** (the trailer `rev` of the
item's `tasks.md` line when the tick was recorded), page, requested at, attempts, state.
Every transition below compares the line's current trailer `rev` with the stored base rev,
so the decision survives a restart and never depends on an earlier parse.

- Each pass, a pending request whose line is still open in `tasks.md` and whose trailer
  `rev` still equals its base rev is re-applied.
- A tick on an item that already has a pending or written request records nothing new (the
  request is keyed by item id).
- A tick on an item whose row is terminal (*applied* or *superseded*), for example one
  re-opened in DOPO and ticked again, **replaces** the row: the new base rev, state
  *pending*, attempts 0. *Stale* rows count as pending.
- **Written** (still pending) when the line shows done but its trailer `rev` still equals
  the base rev. That is Jesinia's own edit, which DOPO hasn't ingested. DOPO may still
  overwrite it with the open version it read earlier, so Jesinia keeps checking. If the line
  goes back to open with the same rev, Jesinia re-applies the edit.
- **Applied** only once DOPO acknowledges the edit: the line shows done with a `rev`
  different from the base rev (DOPO re-renders each line it ingests with a fresh rev), or
  the line has disappeared (DOPO drops a line only when the item is trashed or deleted;
  done items stay as `[x]` lines).
- **Superseded** when the line's `rev` differs from the base rev and the item is still open. DOPO accepted
  some other version (for example, the user re-opened it), so Jesinia stops and lets DOPO
  win.
- **Stale** after 7 days pending (DOPO probably hasn't been opened). Shown in Settings with
  "Open DOPO Tasks to apply"; the request is kept, not dropped.

While a request is pending, the mirror renders that line as `- [x] … · pending DOPO`, so the
tick doesn't appear to bounce back.

### 8.4 Why completion only

Completion is the one KB edit with an unambiguous meaning and a single target token.
Creating actions from the KB (an id-less line with `@project:`) works mechanically. It is
left out because DOPO would file it in the inbox with a project the user picked by typing a
name, which works against triage in DOPO. That can be revisited once Phase 2 has run for a
while.

## 9. Testing

- **Dialect fixtures.** Copy a set of canonical lines (every state, every token, unknown
  tokens, quoted waiting reasons, `*`/`+` bullets, `[X]`) into
  `server/tests/services/tasksmirror/fixtures/`, each with its expected parse. These are
  written by hand from the suite's documented grammar and tests. They are not copied code.
  A suite-side cross-test (like amicus-platform#3212) should pin that DOPO accepts
  Jesinia's completion edit as `ff-db`. That is a follow-up issue there.
- **Domain:** reader, filter, mapping (including NFC/case, collisions, override key),
  region render/replace/first-run, user-text preservation, Today grouping across local
  midnight, completion line edit (status token added/replaced, trailer untouched, every
  other byte identical). 100%.
- **Application:** a pass over fake ports: unchanged input writes nothing; `StaleWrite`
  retry; unreadable file touches no page; ledger transitions (applied, superseded, stale,
  re-applied after an overwrite).
- **Infrastructure:** atomic replace with a concurrent-change retry; mtime poll.
- **Manual:** connect the real DOPO folder, tick an item in Notes with DOPO open, and
  confirm it is done in DOPO within a few seconds; repeat with DOPO closed, then open it.

## 10. Prerequisites and open questions

- **Prerequisite:** the user connects a DOPO sync folder so `tasks.md` exists. No
  `tasks.md` was found on this Mac on 2026-10-22, so this hasn't happened yet.
- **Open:** if the sync folder lives in a cloud-synced directory, a tick made on this Mac
  while DOPO runs on another device reaches DOPO only after that sync. The ledger copes,
  but latency is outside Jesinia's control.
- **Open:** whether the user wants `waiting` items on project pages (included here, labeled)
  or in a separate *Waiting for* list.
- **Follow-ups on `amicus-platform`:** `@today:`/`@priority:` tokens (§6.1); project status
  in `tasks.md`, for example a `## Projects` section or a per-project status line (§4.3); a
  cross-test for external completion edits (§9).
