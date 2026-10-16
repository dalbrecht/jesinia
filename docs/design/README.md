# Jesinia design pass — kit

Everything a design session needs to produce the Jesinia design system and wireframe
every app before any code is written. This is Phase 0 (Tasks 1–6) of
[the foundation plan](../superpowers/plans/2026-10-16-jesinia-foundation.md); the
product context is in [the foundation spec](../specs/2026-10-16-jesinia-foundation-design.md).

## 1. What we are designing for

Jesinia is a personal knowledge base plus a small suite of local apps that feed it. A
Python server on the user's Mac serves the apps as PWAs at `http://127.0.0.1:4870/`.
Every app shares one pattern: **look at something (a page, a post, a PR, an issue) →
think about it with your AI agent in a chat → keep what matters in the KB.**

- **Who:** one technical user (to start), working at a desk, mostly on a laptop display.
  Phones are not a target (the server is localhost-only); narrow layouts exist for small
  windows and side-by-side use.
- **Work shape:** reading-heavy (posts, diffs, notes) with a persistent chat companion.
  Long sessions; calm beats flashy.
- **The KB is plain Markdown** the user also edits in other tools (the Pigeonpile Wiki,
  editors). Jesinia's UI is a lens on those files, not their owner.

## 2. Hard constraints

- **Own identity.** Not Pigeonpile's Daylight language (that is proprietary). It should
  sit comfortably *next to* the Pigeonpile apps without imitating them.
- **Fonts:** OFL, Apache-2.0, or MIT only (the project is AGPL), self-hosted via
  `@fontsource`. No remote font loading (strict CSP, works offline).
- **Tokens:** three tiers — primitives → semantic roles → component tokens — prefixed
  `--jx-`, colors in `oklch`, light and dark themes (`prefers-color-scheme` with a
  `[data-theme]` override). Required semantic roles are listed in plan Task 3.
- **Accessibility:** WCAG 2.2 AA contrast for every text/background pair; full keyboard
  operation; visible focus; reduced-motion respected.
- **Two widths:** desktop (≥ 1200 px) and narrow (390 px). Nothing in between needs a
  bespoke layout.
- **AI-written text is not visually labeled in KB content** (provenance lives in hidden
  Markdown comments). The UI *may* subtly distinguish the agent's chat messages from the
  user's — that is the `--jx-ai` role.

## 3. Deliverables

| Task | Output | Status |
|---|---|---|
| 1 | Pigeonpile Wiki compatibility results → `docs/adr/0007-pigeonpile-compat-results.md` (fixture + procedure in [`compat/`](compat/README.md)) | ☑ passed locally (ADR 0007; cross-test c65llc/amicus-platform#3212) |
| 2 | `docs/design/brief.md` — personality, references, density, fonts, voice | ☑ [brief](brief.md) signed off 2026-10-16 |
| 3 | `docs/design/tokens.md` (+ rendered token sheet) — every token, both themes, contrast table | ☐ |
| 4 | `docs/design/components.md` — eight components, anatomy/variants/states/a11y/props | ☐ |
| 5 | `docs/design/wireframes/` — every screen below, both widths, with an index README | ☐ |
| 6 | `docs/adr/0008-design-system-and-shell.md` — accepted by the user | ☐ |

Visual work can happen as Artifacts (design-system / design types) or as HTML in
`docs/design/wireframes/`; either way, commit the source or an exported snapshot and link
it from the wireframes index.

## 4. Identity session prompts (Task 2)

1. Three to five adjectives Jesinia *is*, and three it *is not*.
2. Two or three products whose reading experience you love; two whose dashboards you
   don't.
3. Density: should RSS/Review feel like a book or a cockpit? Should Issues differ?
4. Default theme — light, dark, or follow system — and is one of them primary?
5. One accent color or a small per-app accent family (launcher tiles, app identity)?
6. Typeface roles: UI sans, long-form reading face (serif?), mono for diffs/code.
7. Motion: none, functional only (streaming, toasts), or some personality?
8. Voice of UI copy: terse/neutral, warm, wry?
9. Does the agent have a visual presence (avatar/glyph/color) or stay unbranded
   ("Claude", "Codex" by name only)?

## 5. Components (Task 4)

Button, Input, Panel, List/Row, Badge, Tabs, Toast, **ChatTranscript**. Every component
needs: default, hover, active, focus-visible, disabled; plus loading where it applies.

ChatTranscript message kinds: user · agent (streaming) · agent (done) · tool use
(collapsed row, expandable) · error (with Retry) · **pending write** (an agent wants to
change the KB — shows a summary and Approve / Reject).

## 6. Shared shell questions (Task 5)

- How do you move between apps (top bar, rail, launcher-only)?
- Where does the **chat pane** live — right rail on desktop, bottom sheet on narrow? Can
  it be popped out or resized?
- Where do **system banners** go: sync paused on a git conflict, KB path missing, agent
  CLI logged out, duplicate page ids found?
- Is search global (⌘K across KB + app content) or per app?
- How does the user pick the agent (Claude Code vs Codex) — per session, per app default,
  global default?

## 7. Screen inventory

States to cover on every screen where they apply: **empty · loading · error ·
streaming · sync paused · runner unavailable · pending write.**

### Foundation (higher fidelity)

**Launcher (`/`)**
- App tiles: Notes, RSS, Review, Issues (later ones shown as "coming soon" until built).
- Status strip: KB path, sync mode + last sync, agent runners available.
- Banner slot (sync paused / KB missing).

**Settings (`/settings/`)**
- KB path: field, Validate, error message, what changing it does (reindex).
- Git sync: detected mode (off / commit only / commit + push), problem text (e.g.
  "detached HEAD"), last run + result, next run, interval (default 4 h), buttons Sync now
  / Resume (only when paused) / Initialize git (only when no `.git`).
- Agents: Claude Code and Codex — available? version, login hint when unavailable;
  default runner.
- Conflicts: duplicate-id files and missing pages, each with paths and a "reveal in
  Finder" affordance.
- Security: rotate token (with consequence copy: re-login, agents re-registered).

**Notes (`/notes/`) — the tracer app**
- Search the KB → results (title, snippet, path).
- Page view: rendered Markdown, `[[links]]`, tags; read-only.
- Chat pane bound to the page: runner picker, transcript, composer.
- Pending-write card inside the chat (agent tried to edit a page).
- Save note: title field, the user's own text, the agent text to keep → result shows the
  new page and its location in `Notes/`.
- Terminal attach indicator: "continued in Claude Code" activity appearing live.

### Products (lower fidelity — inputs to their own specs)

**RSS reader**
- Feeds list (folders, unread counts, add feed / import OPML), item list, reader view
  (sanitized article, original link).
- **Engage**: opens the chat with the post as context; discussion; "Save to KB" creates
  an augmented page in `Sources/`; an engaged item shows that it has a KB page.

**Code review**
- PR picker (repos, PRs awaiting your review).
- **Background**: PR summary, relevant codebase areas, related KB pages / domain notes.
- **Narrative walkthrough** beside the diff: a guided sequence through the change.
- **Metrics**: user-defined criteria with scores and rationale; editing the metric set.
- **Quiz**: questions about what the PR does, how it interacts with the codebase, why it
  is needed; answer, feedback, a comprehension result.
- **Recommendation**: approve / request changes with reasoning — advisory; posting to
  GitHub is a separate, explicit action.

**Issues**
- Repo/project picker; board view (columns by status/label) and list view with filters.
- Issue detail with timeline.
- Triage pane: agent suggestions (labels, assignee, duplicates, priority) with accept /
  dismiss per suggestion; bulk triage.

## 8. Out of scope for this pass

Marketing pages, onboarding beyond `./setup`, notifications, mobile-native shells,
multi-user/sharing UI, logos/brand marks beyond a simple wordmark if one falls out
naturally.
