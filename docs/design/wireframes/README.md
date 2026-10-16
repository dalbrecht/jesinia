# Jesinia wireframes

Phase 0, Task 5 of [the foundation plan](../../superpowers/plans/2026-10-16-jesinia-foundation.md).
These are wireframes for every screen in the [kit's inventory](../README.md#7-screen-inventory),
at desktop (1280) and narrow (390) widths. They're built on the real tokens
([`tokens.css`](tokens.css), generated from [`tokens.md`](../tokens.md) §6) and the
[component specs](../components.md), so they show actual type, color, and density.

**How to view:** serve this folder and open the pages, for example
`python3 -m http.server --directory docs/design 4871`, then visit
<http://127.0.0.1:4871/wireframes/launcher.html>. Each page has a theme toggle (system,
light, dark). The pages load fonts from Google Fonts for preview only; the apps
self-host every face via `@fontsource` (kit §2).

| Page | Fidelity | Shows |
|---|---|---|
| [Launcher](launcher.html) | Foundation (high) | App tiles, status strip, KB missing, sync paused, minor-issue popover, loading, ⌘K palette |
| [Settings](settings.html) | Foundation (high) | KB path, git sync, agents and default runners, conflicts, token rotation; invalid path, validating, paused, no git, rotate confirm, loading, saved |
| [Notes](notes.html) | Foundation (highest) | File tree + search, always-editable page, chat rail, pending write, search results, Save note; changed on disk, compare, pending write on the open page, new note, streaming, error, runner unavailable, terminal attached, sync paused, no results, popped-out chat |
| [RSS](rss.html) | Product (lower) | Three-pane reader, Engage → chat → Save to KB; empty, loading, feed error, streaming, runner unavailable, sync paused |
| [Review](review.html) | Product (lower) | PR picker; Walkthrough beside the diff with chat beneath; Background, Metrics, Quiz, Recommendation; empty, loading pack, GitHub auth, runner unavailable, sync paused |
| [Issues](issues.html) | Product (lower) | Board, triage drawer with suggestions and chat, list with bulk triage, issue detail; empty, loading, GitHub auth, bulk triage in progress, sync paused |
| [Mission Control](overview.html) | n/a | All six windows at thumbnail scale, to check the brief §10 distinctiveness rule |

## Shell decisions

These came from the Task 5 session (2026-10-16) and answer kit §6.

1. **Moving between apps: launcher only.** Each app is a standalone PWA window that owns
   its chrome. There's no app rail or app menu. You switch with the Dock, ⌘Tab, the
   launcher at `/`, or "Open …" commands in the ⌘K palette. The app glyph in the title
   bar links back to the launcher.
2. **Title bar = app header.** Apps use Window Controls Overlay
   (`display_override: ["window-controls-overlay"]`), so the OS traffic lights sit
   inside a single 38 px bar in the app's identity chrome color. The bar holds:
   - the app glyph and name;
   - app-specific controls;
   - the status dot;
   - a ⌕ icon for ⌘K.

   There's no second header row. The bar is a drag region except for its controls.
   Browsers without WCO show the same bar under the normal title bar.
3. **One visible search per window.** Where an app has its own search or filter field
   (the Notes sidebar, the Issues filter), the title bar shows no search field. ⌘K
   always opens the command palette from the keyboard, and the ⌕ icon opens it by
   pointer in the other apps.
4. **Chat: placed per app, can pop out.** Each app puts chat where its work needs it:
   - Notes and RSS: a resizable right rail, 320–640 px.
   - Review: under the walkthrough.
   - Issues: inside the triage drawer.

   Any chat can pop out into its own window bound to the same session ("Dock back"
   returns it). On narrow, chat is a full-height sheet.
5. **System banners by severity.**
   - Blocking conditions (KB missing or unwritable, sync paused, every runner
     unavailable where chat is needed) get a full-width strip under the title bar in
     every app.
   - Minor conditions (duplicate ids, missing pages, one runner unavailable, a fetch
     failing) only turn the status dot amber, with a popover listing them.

   Specified in [components §9.2–9.3](../components.md#92-banner-blocking-system-conditions).
6. **Search: a ⌘K palette in every app.** It covers app content, KB pages, and
   commands, and it replaces in-app navigation. Specified in
   [components §9.1](../components.md#91-commandpalette-k).
7. **Runner choice.** Settings holds the global default runner plus optional per-app
   defaults (Review defaults to Codex in these wireframes). The chat header has a
   runner picker; switching starts a new session on the same subject.

## Spec changes

ADR 0008 (Task 6) must record these. They change the foundation spec:

- **Notes is an editor, not a read-only viewer** (spec §11 says "read-only"). Pages are
  always editable as live-styled Markdown, with no Read/Edit toggle. Specifically:
  - **Saving:** autosave about 1 s after typing stops, as an atomic write that checks
    the base version, plus a changed-on-disk conflict flow (Keep mine, Use theirs,
    Compare).
  - **Front-matter and provenance:** front-matter shows as a properties line with the
    `id` read-only. Provenance comments fold into chips that can't be edited.
  - **Links:** `[[` autocomplete, including "Create".
  - **Toolbar:** no permanent toolbar on desktop; a floating toolbar appears on
    selection.
  - **Implications:** this needs `kb.write` from the PWA for user edits, including base
    version or hash concurrency. It also needs an editor library (CodeMirror 6, MIT, is
    the likely choice for Task 22/25) and a Markdown live-preview mode.
- **Notes has a file tree** alongside search, which needs a KB folder listing API
  (folders, page counts, move or rename that rewrites links through `kb.*`).
- **Notes search results** move to the main column beside the tree. They use compact
  density, group titles before content, and offer folder scope and sort.
- **Three shell components** are added to the design package: CommandPalette, Banner,
  and StatusIndicator (components §9). The plan's Task 22 export list is updated.
- **The PWA manifest** gains `display_override: ["window-controls-overlay"]`, and the
  shell (Task 23 `AppShell`) renders the title-bar header described above.
  `theme_color` is each app's `-chrome` value (tokens §3).

## State coverage

States required by kit §7 are listed per screen. "n/a" means the state can't occur
there.

| State | Launcher | Settings | Notes | RSS | Review | Issues |
|---|---|---|---|---|---|---|
| empty | n/a (static tiles) | n/a | no results; no page open | no feeds | no PRs | no repo |
| loading | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| error | KB missing | invalid path | mid-stream error | feed 404 | GitHub auth | GitHub auth |
| streaming | n/a | n/a | ✓ | ✓ | ✓ | bulk triage |
| sync paused | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| runner unavailable | popover | agents panel | ✓ | ✓ | ✓ | n/a (Claude default) |
| pending write | n/a | n/a | ✓ (also on the open page) | n/a (Save to KB is the user's action) | n/a | suggestions use Accept/Dismiss |
