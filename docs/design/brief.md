# Jesinia design brief

Phase 0, Task 2 of [the foundation plan](../superpowers/plans/2026-10-16-jesinia-foundation.md).
Decisions from the identity session of 2026-10-16. They answer the prompts in the
[design-pass kit](README.md#4-identity-session-prompts-task-2). Tasks 3–6 (tokens,
components, wireframes, ADR 0008) build on this brief. Where a later task needs a choice
this brief doesn't make, it should extend the brief, not contradict it.

The visual explorations used in the session are committed under
[`explorations/`](explorations/README.md).

## 1. Personality

**Jesinia is:** calm · studious · precise · warm · unhurried, and it should feel
**crafted and human, with a little personality**.

**Jesinia is not:** flashy · gamified · corporate.

The image is a well-lit study or workshop bench: good paper, a reading lamp, tools laid
out with care. The UI should feel made by a person who reads a lot, not generated from a
component kit. Personality comes from type, tone, and small considered details. Chrome,
gradients, and decoration don't carry it.

## 2. References

| | Products | What we take / avoid |
|---|---|---|
| **Reading we love** | Reeder / NetNewsWire | Quiet lists, a typographic article view, minimal chrome |
| | Readwise Reader / Instapaper | Serif long-form in a focused column, with a side panel for notes and highlights |
| | Linear / Things | Calm, precise app chrome, crisp lists, restrained color, keyboard flow |
| **Dashboards we don't want** | Jira | Chrome everywhere, competing colors and badges, noise |
| | Salesforce / HubSpot | Corporate blue, card soup, banners, upsell energy |

**Pigeonpile.** Jesinia must sit comfortably next to the Pigeonpile apps without
borrowing Daylight's styling, which is proprietary. Nothing in this brief derives from
Daylight tokens, components, or assets.

## 3. Density

- **RSS and Review: "Desk" density.** List rows use the UI sans, with a title, a one-line
  excerpt, and source and age. Roughly 7–8 items fit per laptop screen. The reading face
  is kept for the reader view, the review walkthrough, and KB pages, not list rows.
- **Issues: compact.** Issues is operational (tables, labels, assignees), so it runs one
  step denser than RSS and Review. It keeps the same families and palette and changes
  only row padding and size.
- **No user density toggle** in the foundation. Task 3 should express density as
  component tokens (row padding, row text size) so a toggle can be added later without
  restyling.

## 4. Theme

- **Follow the system** (`prefers-color-scheme`), with a `[data-theme]` override in
  Settings.
- **Light and dark are equals.** Neither is derived from the other. Task 3 designs both
  side by side, and every token gets a hand-tuned value in each theme. Dark is a deep
  green-grey "night herbarium", not inverted light and not pure black.

## 5. Color: the Botanical plate palette

A small family of tones, like a hand-coloured herbarium print. It is not one accent over
grey. Draft anchors are below in oklch. **They are rough. Task 3 tunes every value to
WCAG 2.2 AA and builds the 50–950 ramps.**

| Role | Hue | Draft light | Draft dark | Used for |
|---|---|---|---|---|
| **Primary**: sage | ~155 | `oklch(46% 0.075 155)` | `oklch(76% 0.08 155)` | Buttons, links, focus, selection fill (as a tint) |
| **Secondary**: marigold | ~80 | `oklch(72% 0.13 80)` | (tune) | Reading highlights, markers, the occasional warm touch. Never body text on light backgrounds |
| **Label set** | marigold ~80 · plum ~340 · cornflower ~260 · clay ~40 · fern ~165 | tinted pill backgrounds, darker text | dark pill, lighter text | KB tags, Issues labels, categories |
| **Neutrals**: rag paper | ~120–150, very low chroma | bg `oklch(97.4% 0.007 120)`, surface `oklch(99.3% 0.004 120)` | bg `oklch(19% 0.012 150)`, surface `oklch(23% 0.013 150)` | Backgrounds, surfaces, borders, text. Faintly green-grey, never pure grey |
| **`--jx-ai`** | primary hue, very low chroma | `oklch(95.3% 0.018 150)` | `oklch(27% 0.025 155)` | Background of the agent's chat messages only. Never KB content |

**Accent strategy: one interactive primary plus per-app identity hues.**

- Sage is the only interactive color: buttons, links, focus, and selection.
- Each app has an identity hue. It appears on the app's launcher tile, its glyph, and its
  PWA icon and `theme_color`, and it may tint the app's own chrome (see §10). It is never
  used for buttons, links, focus, or body text.

  | App | Identity hue |
  |---|---|
  | Notes | sage ~155 |
  | RSS | marigold ~80 |
  | Review | plum ~340 |
  | Issues | cornflower ~260 |

**Rules.**

- **Selection is a soft tinted fill** (primary at low chroma), with a slightly heavier
  title weight. **No colored left borders or accent rails**, on rows or on cards.
- Semantic status colors are separate from the palette and never rely on color alone.
  Each status color always comes with an icon or a word. Task 3 has to resolve three
  near-collisions:
  - **success** vs sage primary and the fern label: shift success toward a bluer green,
    or rely on the icon.
  - **warn** vs marigold: shift warn toward orange (~55).
  - **danger** vs the clay label: keep danger a clearer red (~25), and push clay toward
    brown.
- Neutrals are tinted. No `#808080`-style mid greys anywhere.

## 6. Typography

All three families are **OFL-1.1**. All three are on @fontsource as static and variable
packages, self-hosted, with no remote font loading. License and availability were checked
against the Fontsource API on 2026-10-16.

| Role | Family | Package | Weights / axes | Used for |
|---|---|---|---|---|
| **UI sans** (`--jx-font-ui`) | **Bricolage Grotesque** | `@fontsource-variable/bricolage-grotesque` | wght 400 · 500 · 600; `opsz` 12–96 (auto); `wdth` left at 100 | Chrome, lists, forms, buttons, chat, headings inside app UI |
| **Reading** (`--jx-font-read`) | **Newsreader** | `@fontsource-variable/newsreader` | wght 400 · 600, italic 400 · 600; `opsz` 6–72 (auto) | Rendered KB pages, the RSS reader view, the Review walkthrough narrative, long agent answers when shown "as a page" |
| **Mono** (`--jx-font-mono`) | **JetBrains Mono** | `@fontsource-variable/jetbrains-mono` | wght 400 · 500, italic 400 | Diffs, code blocks, inline code, paths, ids |

**Notes.**

- **Bricolage has no italic.** UI emphasis uses weight (500/600). Set
  `font-synthesis: none` on UI text so the browser never fakes an oblique. Italic appears
  only in Newsreader content and JetBrains Mono.
- Bricolage's quirks come out at large optical sizes. Keep `font-optical-sizing: auto` so
  small UI text stays calm and headings get the character. Avoid weights above 600 in the
  UI. The wordmark, if one is made, may go heavier.
- **Subsets:** ship `latin` + `latin-ext` only, to keep PWA payload small.
- Fallback stacks: UI `system-ui, -apple-system, sans-serif`; reading
  `Charter, Georgia, serif`; mono `ui-monospace, Menlo, monospace`.
- Reading measure stays around 60–70 characters. Task 3 sets the scale and line heights.

## 7. Motion

**Functional, plus a little warmth.**

- Motion only explains state change: streamed text arriving, toasts entering and leaving,
  panels opening, the pending-write card appearing, list items being added or removed.
- Two durations (`--jx-duration-fast` ~120 ms, `--jx-duration-base` ~200 ms) and one
  gentle ease-out (`--jx-ease`). No bounces or springs.
- A small "human" touch is allowed where it marks a meaningful moment, for example a soft
  settle when a note saves or when a pending write is approved. At most one per flow.
- **No** entrance choreography, parallax, looping decoration, or animated "thinking"
  mascots. The streaming indicator is a quiet cursor or ellipsis.
- `prefers-reduced-motion: reduce` turns all transitions into instant changes or plain
  opacity fades. Streaming text still appears, but without animation.

## 8. Agent visual presence

**Name plus a small neutral glyph per runner.**

- Each runner has a Jesinia-drawn mark: a monogram ("C" for Claude Code, "X" for Codex)
  in a soft rounded square. Each mark uses its own label-set tone, distinct from the app
  hues and from sage. The name ("Claude", "Codex") appears beside it in agent message
  headers, the runner picker, and Settings.
- **No vendor logos.** That avoids trademark rules and keeps the visual language our own.
- Agent messages sit on the `--jx-ai` tint. User messages sit on a plain surface with a
  border. This is the only visual difference between them.
- **KB content is never visually marked as AI-written.** Provenance stays in hidden
  Markdown comments (spec §5).

## 8a. App icons

Every app icon follows one convention, added 2026-10-16 during Task 5. The icon is a
**zoomed-in macro image of a flower, plant, or animal in the app's identity hue**, with
a **contrasting monochrome symbol** for the app centered on top.

| App | Macro subject | Hue | Symbol |
|---|---|---|---|
| Notes | Unfurling fern fiddlehead | sage ~155 | Pencil |
| RSS | Honeybee fur dusted with pollen | marigold ~80 | Feed arcs |
| Review | Jewel-beetle wing case (elytra) | plum ~340 | Magnifier with check |
| Issues | Morpho butterfly wing scales | cornflower ~260 | Board columns |
| Launcher | Grey-green lichen on stone | near-neutral paper | Four tiles, one filled |
| Settings | Grey feather barbs | near-neutral paper | Sliders |

**Image**

- True macro: texture fills the frame (petals, fur, scales, fronds, barbs). At icon
  size it reads as color and pattern, not as a recognizable subject.
- Graded toward the app hue with a light duotone, so the set reads as one family.
- Quiet in the center: shallow depth of field or soft texture where the symbol sits,
  plus a gentle vignette.
- **Source: AI-generated.** Each image records its model, prompt, and date in
  `docs/design/icons/CREDITS.md`. No stock or third-party photos, so there are no
  redistribution questions for the AGPL repo.

**Symbol**

- Monochrome: paper-white on dark or saturated images, ink on light ones.
- **Heavy:** drawn on a 24-unit grid, using filled shapes wherever they read well and
  3.2–3.6 unit round strokes otherwise. It fills about 66% of the tile, optically
  centered, inside the 80% maskable safe zone.
- At least 3:1 contrast against the image under it, helped by the vignette and a
  1–1.5 px soft shadow. Checked at 48 px.
- The same glyph is the app's title-bar glyph and its PWA `monochrome` icon.

**Files and sizes**

- **Masters:** 1024 px square, full bleed, with no baked-in corner radius or shadow
  (the OS applies its mask). The symbol stays inside the central 80% maskable safe
  zone.
- **Manifest:** 192 and 512 `any`, 512 `maskable`, and an SVG `monochrome` icon.
- **32 px and below** (favicon, tabs): no image; a flat `--jx-app` fill plus the
  symbol.
- **16 px:** the symbol alone.
- **Layout:** `docs/design/icons/<app>/` holds the source image, graded master, symbol
  SVG, and exports.

## 9. UI voice

Jesinia's copy is **plain, warm, and short**. Write like a knowledgeable colleague at the
next desk. Say what happened and what to do next in as few words as stay clear: "Sync
paused: merge conflict in Notes/rss.md. Resolve it, then choose Resume." Name things the
way the user sees them (pages, feeds, notes, sync), not how the system is built (index,
runner process, MCP). Buttons state the action ("Save note", "Approve change"), and the
confirmation echoes it ("Note saved"). Errors give the cause and the fix with no
apologies, no exclamation marks, and no blame. Prefer the specific detail (a file path, a
count, a time) over reassurance. Warmth comes from being considerate and exact: plain
verbs, sentence case, and the occasional kind word in empty states. Avoid jokes, cute
phrasing, and filler. Refer to agents by name ("Claude wants to edit this page"), never as
"the AI".

## 10. App latitude and distinctiveness

The design system is a **shared foundation, not a uniform**. Each PWA has real freedom to
riff on it, for experimentation and so each app has its own character.

**Every app must be recognizable at a glance**, including as a thumbnail in macOS
Mission Control (Exposé), where windows shrink so far that text is unreadable. Each app
needs a distinct silhouette made from its identity hue, its layout shape, and its chrome
treatment. Notes, RSS, Review, and Issues should never look like the same window with
different words in it.

**Fixed in every app (the contract):**

- `--jx-` semantic tokens for text, surfaces, and status, and the WCAG 2.2 AA contrast
  they guarantee, in both themes.
- Sage as the only interactive color, and the shared focus ring.
- The shared components (Button, Input, Panel, List/Row, Badge, Tabs, Toast,
  ChatTranscript). Their behavior, keyboard model, and ARIA are not overridden. Apps may
  restyle them only through component tokens.
- The three type families. Apps may use them differently but don't add new ones.
- The motion stance, reduced-motion handling, and the UI voice.
- The shared shell: app switching, system banners, and the chat pane's docking rules
  (Task 5).

**Open for each app to riff on:**

- **Identity-hue chrome:** a tinted header band, sidebar, or canvas wash in the app's hue.
  This is the main Exposé cue.
- **Layout shape:** column count, pane proportions, and where content sits. RSS can be a
  three-pane reader, Review a split diff and narrative, Issues a board.
- **Display typography:** how the app uses Bricolage's `opsz` and `wdth` axes and
  Newsreader's display sizes for its own headings and wordmark.
- **App-specific components and visual devices** built from tokens, such as a quiz card,
  a metrics scale, or a board column. Repeated devices may graduate into
  `@jesinia/design` later.
- **App-level component token overrides** (radius, density, surface tints) scoped to the
  app root.

**How it's enforced:** each app sets its identity tokens and overrides on its root
element. Task 3 defines which tokens are app-overridable (`--jx-app-*`, plus a named
subset of component tokens). Task 22's contrast test runs against each app's overrides
too. Each app's wireframes (Task 5) show its distinct silhouette, including a
thumbnail-scale view of all four apps side by side.
