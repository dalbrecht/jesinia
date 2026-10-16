# ADR 0008: Design system and app shell

- **Status:** Accepted (approved by the user, 2026-10-16). Code phases may start.
- **Date:** 2026-10-16
- **Related:**
  - foundation spec §1, §4.1, §9, §11 (updated in this change);
  - plan Phase 0, Tasks 2–6;
  - [brief](../design/brief.md), [tokens](../design/tokens.md),
    [components](../design/components.md), [wireframes](../design/wireframes/README.md),
    [app icons](../design/icons/index.html).

## Context

The foundation spec (§9) planned a neutral design-system scaffold, with the visual
identity left to a separate design pass. Phase 0 ran that pass before any code. These
were all reviewed and merged with the user's sign-off:

- the identity brief (Task 2, PR #2);
- tokens (Task 3, PR #3);
- components (Task 4, PR #4);
- wireframes and the app icon set (Task 5, PR #5).

The pass also changed what the foundation builds, mainly the Notes app. This ADR records
the result as one decision so implementation (Phases 1–7) has one reference, and it
updates the spec where the two differ.

## Decision

### 1. Identity ([brief](../design/brief.md))

- **Personality:** calm, studious, precise, warm, unhurried. The UI should feel crafted
  and human, with a little personality. It is not flashy, gamified, or corporate. It has
  its own identity: no Pigeonpile Daylight styling is used.
- **Density:** "Desk" for reading apps (Notes, RSS, Review); compact for Issues.
- **Theme:** follows the system, with a `[data-theme]` override. Light and dark are
  designed as equals.
- **Palette ("Botanical plate"):**
  - sage is the only interactive color;
  - marigold is for reading highlights;
  - a five-tone label set;
  - rag-paper neutrals;
  - one identity hue per app (Notes sage, RSS marigold, Review plum, Issues
    cornflower), used only for the app's title bar, glyph, and icon.
- **Type:** Bricolage Grotesque for UI, Newsreader for reading, JetBrains Mono for code.
  All three are OFL-1.1 and self-hosted via `@fontsource`.
- **Motion and voice:**
  - Motion is functional, with a little warmth, and respects reduced motion.
  - Copy is plain, warm, and concise.
- **App latitude:** apps may riff on the system within a fixed contract (brief §10), and
  every app must be recognizable at Mission Control thumbnail size.
- **App icons:** an AI-generated macro image in the app's hue, with a heavy monochrome
  symbol on top (brief §8a). The symbol is also the title-bar glyph and the PWA
  `monochrome` icon.

### 2. Tokens ([tokens.md](../design/tokens.md))

- **Three tiers:** primitives `--jx-<hue>-<step>` → semantic roles → component tokens.
  All are oklch, inside the sRGB gamut.
- **Primitives:** 111, covering ten ramps.
- **Semantic roles:** 50, hand-assigned per theme.
- **Component tokens** cover all eight core components, plus app-overridable tokens
  scoped to each app's root.
- **Contrast:** a table of 65 text/background pairs, all WCAG 2.2 AA in both themes.
  It is the fixture for Task 22's contrast test.
- **Generated output:** [`wireframes/tokens.css`](../design/wireframes/tokens.css) is
  generated from the same source and is identical to tokens.md §6.

### 3. Components ([components.md](../design/components.md))

There are eleven shared components, each specified with anatomy, variants, states,
tokens, keyboard behavior, ARIA, and React props:

- **Core:** Button, Input, Panel, List/Row, Badge, Tabs, Toast, ChatTranscript. The
  transcript has six message kinds, a pending-write state machine, an auto-scroll rule,
  and a live-region policy.
- **Shell** (added in Task 5): CommandPalette (⌘K), Banner, StatusIndicator.

components.md is authoritative for exported names and props. The plan's Task 22 defers
to it.

### 4. Shell ([wireframes README](../design/wireframes/README.md#shell-decisions))

1. **Launcher-only navigation.** Each app is a standalone PWA window and owns its chrome.
   There is no app rail. ⌘K "Open …" commands and the launcher at `/` switch between
   apps.
2. **The title bar is the app header.** Window Controls Overlay
   (`display_override: ["window-controls-overlay"]`) gives one 38 px bar in the app's
   identity color. It holds the glyph and name, app controls, the status dot, and a ⌕
   icon.
3. **One visible search per window.** ⌘K always opens the command palette.
4. **Chat is placed per app and can pop out.**
   - Notes and RSS: a resizable right rail.
   - Review: under the walkthrough.
   - Issues: inside the triage drawer.
   - Narrow: a full-height sheet.
5. **Banners by severity.** Blocking conditions get a full-width strip:
   - KB missing or unwritable;
   - sync paused;
   - no runner available where one is needed.

   Minor conditions only turn the status dot amber, with a popover that lists them.
6. **Runner choice.** A global default in Settings, an optional per-app default, and a
   picker in the chat header. Switching starts a new session.

### 5. Notes is an editor

The tracer app goes beyond the spec's read-only page view:

- **Always editable.** Live-styled Markdown, with markers shown only on the caret's line
  and no Read/Edit toggle.
- **Autosave.** About 1 s after typing stops (and on blur or page switch), as an atomic
  write that checks the base version. If the file changed on disk, autosave stops and
  offers Keep mine, Use theirs, or Compare.
- **Front-matter and provenance.** Front-matter shows as a properties line with the `id`
  read-only. `jesinia:ai` provenance comments fold into chips that can't be edited.
- **Links.** Typing `[[` opens autocomplete, including "Create".
- **No permanent toolbar on desktop.** Formatting uses Markdown shortcuts and a floating
  toolbar on selection.
- **File tree** next to search: compact, with page counts. It can create notes and
  folders. Links resolve by id, so moving a file never breaks them; renaming updates
  the title text of inbound links via `kb.*`.
- **Search results** fill the main column: compact density, titles grouped before
  content, with folder scope and sort.
- **Pending writes on the open page** apply as one undoable edit, so unsaved typing is
  never lost.

## Consequences

**Spec changes in this commit**

- **§1 non-goals:** the final visual identity is no longer deferred; it's defined by this
  ADR.
- **§4.1 `kb`:** writes accept an optional base version and fail with a conflict when
  the file has changed. Body-only updates keep the raw front-matter bytes, so editor
  saves never reformat user YAML. `kb.tree` lists folders with page counts, and a
  rename updates the title text of inbound `[[id|Title]]` links.
- **§9:** the design system is now the full identity: eleven components, the shell
  pattern, and app icons. Its values are no longer placeholders.
- **§11:** Notes edits pages and has a file tree.

**Plan changes in this commit**

- **Tasks 10–12 (KB service):**
  - Task 10 adds raw front-matter split and body replacement, and link retitling.
  - Task 11 adds `update_body`, `tree`, and relinking on rename.
  - Task 12 adds `GET /api/kb/tree`, a body-only `PATCH` (409 on conflict), and the
    `kb_tree` MCP tool.
- **Task 24:** Notes consumes those shared KB routes and adds no KB access of its
  own.
- **Task 25:**
  - adds the editor and the tree;
  - adds e2e steps for autosave and the changed-on-disk flow;
  - names CodeMirror 6 (MIT) as the editor library, to be confirmed while implementing.
- **Task 23:** its shell (`AppShell`, manifest) follows §4 of this ADR, and each app's
  icons are exported from `docs/design/icons/`.

**Risks and follow-ups**

- **Editing makes Notes a second writer to KB files**, alongside external editors and
  the Pigeonpile Wiki. The base-version check and the changed-on-disk flow are the
  safeguard. ADR 0007's compatibility guarantees (front-matter and provenance preserved)
  now also apply to Jesinia's own editor, and Task 25's e2e must assert them.
- **Window Controls Overlay** is supported only in Chromium-based installed PWAs. Other
  browsers show the same bar under a normal title bar, which costs 28 px but loses no
  function.
- **The app icon images are AI-generated** (Codex image generation). Their prompts are
  in `icons/CREDITS.md`, so they can be regenerated or replaced without changing the
  convention.
- **Two decisions are deferred to the product specs:** per-repo storage of Review metrics,
  and selectable Issues rows that contain checkboxes (a conflict with components.md
  List/Row, which must be resolved there).
