# Jesinia design tokens

Phase 0, Task 3 of [the foundation plan](../superpowers/plans/2026-10-16-jesinia-foundation.md).
Builds on [the brief](brief.md). Every value in this file is exact. `@jesinia/design`
(Task 22) implements it verbatim, and the [contrast table](#5-contrast-table) is the
fixture for Task 22's contrast test.

**Rendered sheet:** [`tokens.html`](tokens.html) shows every primitive, both themes side
by side, the components' token usage, and the contrast table. Open it directly in a
browser.

## 1. Rules

- **Three tiers.** Primitives (`--jx-<hue>-<step>`) → semantic roles (`--jx-bg`,
  `--jx-accent`, …) → component tokens (`--jx-button-primary-bg`, …). Components read
  only component or semantic tokens, never primitives. Apps read semantic and component
  tokens only.
- **Prefix** `--jx-`. **Colors in `oklch`.** Every primitive is inside the sRGB gamut.
  Chroma was reduced where a hue would otherwise clip.
- **Themes:** light is the default on `:root`. Dark applies under
  `prefers-color-scheme: dark` unless `[data-theme="light"]` is set, and always under
  `[data-theme="dark"]`. Both themes are hand-assigned from the same primitives.
  Neither is computed from the other.
- **No alpha in semantic colors.** Every text/background pair has a single computed
  contrast ratio. Shadows are the only translucent tokens.
- **Component tokens re-resolve per theme.** They're declared on `:root, [data-theme]`,
  because a custom property that references another one is resolved where it's
  declared and then inherited as a fixed value.
- **Selection is a fill** (`--jx-accent-subtle`) plus a heavier title weight. No
  accent borders or rails on rows or cards.
- **Status never relies on color alone.** Success, warn, and danger always come with an
  icon or a word.

## 2. Primitives

Steps 50–950 share one lightness ladder across hues, so the same step in different hues
has comparable lightness and contrast:

| Step | 50 | 100 | 200 | 300 | 400 | 500 | 600 | 700 | 800 | 900 | 950 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| L | 97.5% | 95% | 90% | 83% | 74% | 64% | 54% | 45% | 36% | 27% | 21% |

`paper` (the neutral) has its own ladder plus a `0` step for the lightest raised
surface. Its hue drifts from ~120 (light end) to ~150 (dark end), so light surfaces read
as rag paper and dark ones as a deep green-grey.

| Ramp | Hue | Role |
|---|---|---|
| paper | 120→150 | Neutrals: backgrounds, surfaces, text, borders |
| sage | 155 | primary |
| marigold | 80 | secondary / label / RSS |
| plum | 340 | label / Review |
| cornflower | 262 | label / Issues |
| clay | 52 | label / Claude glyph |
| fern | 128 | label / Codex glyph |
| jade | 185 | success |
| persimmon | 55 | warn |
| madder | 25 | danger |

**Changes from the brief's draft hues (resolving its collision notes):**

- fern moved from ~165 to **128**, a yellow-green frond, so it no longer sits on top of
  sage (155).
- success is **jade 185**, 30° bluer than sage.
- warn is **persimmon 55**, more orange than marigold (80).
- danger is **madder 25**.
- clay moved to **52** at low chroma, so it reads as brown rather than as a second red.

<details><summary><code>paper</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-paper-0` | `oklch(99.8% 0.002 120)` | `#fefffd` |
| `--jx-paper-50` | `oklch(99.2% 0.004 122)` | `#fcfdfa` |
| `--jx-paper-100` | `oklch(97.3% 0.007 123)` | `#f5f7f2` |
| `--jx-paper-200` | `oklch(93.5% 0.010 126)` | `#e8ebe4` |
| `--jx-paper-300` | `oklch(88.5% 0.012 129)` | `#d7dbd2` |
| `--jx-paper-400` | `oklch(78% 0.013 133)` | `#b4b9b1` |
| `--jx-paper-500` | `oklch(60% 0.014 136)` | `#7c827a` |
| `--jx-paper-600` | `oklch(49% 0.014 139)` | `#5c625b` |
| `--jx-paper-700` | `oklch(38% 0.014 142)` | `#3e443d` |
| `--jx-paper-800` | `oklch(29% 0.013 145)` | `#272d27` |
| `--jx-paper-900` | `oklch(23.5% 0.013 148)` | `#1a201b` |
| `--jx-paper-950` | `oklch(19.5% 0.012 150)` | `#111612` |

</details>

<details><summary><code>sage</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-sage-50` | `oklch(97.5% 0.013 155)` | `#f0faf3` |
| `--jx-sage-100` | `oklch(95% 0.022 155)` | `#e4f3e8` |
| `--jx-sage-200` | `oklch(90% 0.041 155)` | `#cae7d2` |
| `--jx-sage-300` | `oklch(83% 0.063 155)` | `#a8d4b6` |
| `--jx-sage-400` | `oklch(74% 0.081 155)` | `#81ba95` |
| `--jx-sage-500` | `oklch(64% 0.090 155)` | `#5d9c74` |
| `--jx-sage-600` | `oklch(54% 0.090 155)` | `#3f7e57` |
| `--jx-sage-700` | `oklch(45% 0.081 155)` | `#2b6241` |
| `--jx-sage-800` | `oklch(36% 0.068 155)` | `#1a472d` |
| `--jx-sage-900` | `oklch(27% 0.050 155)` | `#0f2e1b` |
| `--jx-sage-950` | `oklch(21% 0.036 155)` | `#091d11` |

</details>

<details><summary><code>marigold</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-marigold-50` | `oklch(97.5% 0.022 80)` | `#fff6e7` |
| `--jx-marigold-100` | `oklch(95% 0.037 80)` | `#fcecd3` |
| `--jx-marigold-200` | `oklch(90% 0.068 80)` | `#f6daab` |
| `--jx-marigold-300` | `oklch(83% 0.105 80)` | `#ebc076` |
| `--jx-marigold-400` | `oklch(74% 0.135 80)` | `#d7a035` |
| `--jx-marigold-500` | `oklch(64% 0.132 80)` | `#b68103` |
| `--jx-marigold-600` | `oklch(54% 0.111 80)` | `#906603` |
| `--jx-marigold-700` | `oklch(45% 0.093 80)` | `#704e00` |
| `--jx-marigold-800` | `oklch(36% 0.074 80)` | `#513801` |
| `--jx-marigold-900` | `oklch(27% 0.056 80)` | `#352300` |
| `--jx-marigold-950` | `oklch(21% 0.044 80)` | `#231600` |

</details>

<details><summary><code>plum</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-plum-50` | `oklch(97.5% 0.016 340)` | `#fff3fa` |
| `--jx-plum-100` | `oklch(95% 0.030 340)` | `#fde7f5` |
| `--jx-plum-200` | `oklch(90% 0.054 340)` | `#f8d1e9` |
| `--jx-plum-300` | `oklch(83% 0.084 340)` | `#edb2d9` |
| `--jx-plum-400` | `oklch(74% 0.108 340)` | `#d990c0` |
| `--jx-plum-500` | `oklch(64% 0.120 340)` | `#bc6ea3` |
| `--jx-plum-600` | `oklch(54% 0.120 340)` | `#9b5084` |
| `--jx-plum-700` | `oklch(45% 0.108 340)` | `#7b3a68` |
| `--jx-plum-800` | `oklch(36% 0.090 340)` | `#5b284c` |
| `--jx-plum-900` | `oklch(27% 0.066 340)` | `#3b1830` |
| `--jx-plum-950` | `oklch(21% 0.048 340)` | `#260f1f` |

</details>

<details><summary><code>cornflower</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-cornflower-50` | `oklch(97.5% 0.011 262)` | `#f3f7fe` |
| `--jx-cornflower-100` | `oklch(95% 0.023 262)` | `#e6efff` |
| `--jx-cornflower-200` | `oklch(90% 0.048 262)` | `#cddfff` |
| `--jx-cornflower-300` | `oklch(83% 0.084 262)` | `#aac8ff` |
| `--jx-cornflower-400` | `oklch(74% 0.108 262)` | `#85abef` |
| `--jx-cornflower-500` | `oklch(64% 0.120 262)` | `#638bd5` |
| `--jx-cornflower-600` | `oklch(54% 0.120 262)` | `#476db4` |
| `--jx-cornflower-700` | `oklch(45% 0.108 262)` | `#335391` |
| `--jx-cornflower-800` | `oklch(36% 0.090 262)` | `#223b6c` |
| `--jx-cornflower-900` | `oklch(27% 0.066 262)` | `#142546` |
| `--jx-cornflower-950` | `oklch(21% 0.048 262)` | `#0c182e` |

</details>

<details><summary><code>clay</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-clay-50` | `oklch(97.5% 0.011 52)` | `#fdf5f0` |
| `--jx-clay-100` | `oklch(95% 0.019 52)` | `#faebe3` |
| `--jx-clay-200` | `oklch(90% 0.034 52)` | `#f1d8ca` |
| `--jx-clay-300` | `oklch(83% 0.052 52)` | `#e4bfa9` |
| `--jx-clay-400` | `oklch(74% 0.068 52)` | `#ce9f84` |
| `--jx-clay-500` | `oklch(64% 0.075 52)` | `#b17f62` |
| `--jx-clay-600` | `oklch(54% 0.075 52)` | `#926245` |
| `--jx-clay-700` | `oklch(45% 0.068 52)` | `#734a31` |
| `--jx-clay-800` | `oklch(36% 0.056 52)` | `#543420` |
| `--jx-clay-900` | `oklch(27% 0.041 52)` | `#362013` |
| `--jx-clay-950` | `oklch(21% 0.030 52)` | `#23140b` |

</details>

<details><summary><code>fern</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-fern-50` | `oklch(97.5% 0.017 128)` | `#f3f9ed` |
| `--jx-fern-100` | `oklch(95% 0.028 128)` | `#e9f2df` |
| `--jx-fern-200` | `oklch(90% 0.050 128)` | `#d4e5c2` |
| `--jx-fern-300` | `oklch(83% 0.077 128)` | `#b9d19c` |
| `--jx-fern-400` | `oklch(74% 0.099 128)` | `#99b772` |
| `--jx-fern-500` | `oklch(64% 0.110 128)` | `#79994d` |
| `--jx-fern-600` | `oklch(54% 0.110 128)` | `#5c7a2e` |
| `--jx-fern-700` | `oklch(45% 0.099 128)` | `#455f1b` |
| `--jx-fern-800` | `oklch(36% 0.083 128)` | `#30450d` |
| `--jx-fern-900` | `oklch(27% 0.061 128)` | `#1e2c07` |
| `--jx-fern-950` | `oklch(21% 0.044 128)` | `#121c05` |

</details>

<details><summary><code>jade</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-jade-50` | `oklch(97.5% 0.014 185)` | `#edfaf8` |
| `--jx-jade-100` | `oklch(95% 0.024 185)` | `#def4f0` |
| `--jx-jade-200` | `oklch(90% 0.043 185)` | `#bfe8e2` |
| `--jx-jade-300` | `oklch(83% 0.066 185)` | `#96d6cd` |
| `--jx-jade-400` | `oklch(74% 0.086 185)` | `#66bdb2` |
| `--jx-jade-500` | `oklch(64% 0.095 185)` | `#389f94` |
| `--jx-jade-600` | `oklch(54% 0.095 185)` | `#018076` |
| `--jx-jade-700` | `oklch(45% 0.079 185)` | `#01635b` |
| `--jx-jade-800` | `oklch(36% 0.063 185)` | `#014841` |
| `--jx-jade-900` | `oklch(27% 0.047 185)` | `#012e29` |
| `--jx-jade-950` | `oklch(21% 0.037 185)` | `#001e1a` |

</details>

<details><summary><code>persimmon</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-persimmon-50` | `oklch(97.5% 0.014 55)` | `#fff5ee` |
| `--jx-persimmon-100` | `oklch(95% 0.029 55)` | `#ffeadd` |
| `--jx-persimmon-200` | `oklch(90% 0.061 55)` | `#ffd4b8` |
| `--jx-persimmon-300` | `oklch(83% 0.105 55)` | `#fdb584` |
| `--jx-persimmon-400` | `oklch(74% 0.135 55)` | `#eb9251` |
| `--jx-persimmon-500` | `oklch(64% 0.150 55)` | `#cf6f19` |
| `--jx-persimmon-600` | `oklch(54% 0.133 55)` | `#a75502` |
| `--jx-persimmon-700` | `oklch(45% 0.111 55)` | `#834101` |
| `--jx-persimmon-800` | `oklch(36% 0.089 55)` | `#5f2e00` |
| `--jx-persimmon-900` | `oklch(27% 0.067 55)` | `#3e1c00` |
| `--jx-persimmon-950` | `oklch(21% 0.052 55)` | `#2a1000` |

</details>

<details><summary><code>madder</code></summary>

| Token | Value | sRGB |
|---|---|---|
| `--jx-madder-50` | `oklch(97.5% 0.012 25)` | `#fff4f3` |
| `--jx-madder-100` | `oklch(95% 0.024 25)` | `#fee9e6` |
| `--jx-madder-200` | `oklch(90% 0.051 25)` | `#fed2cd` |
| `--jx-madder-300` | `oklch(83% 0.094 25)` | `#ffb0a9` |
| `--jx-madder-400` | `oklch(74% 0.144 25)` | `#f8837c` |
| `--jx-madder-500` | `oklch(64% 0.160 25)` | `#dc5e59` |
| `--jx-madder-600` | `oklch(54% 0.160 25)` | `#ba3e3c` |
| `--jx-madder-700` | `oklch(45% 0.144 25)` | `#952929` |
| `--jx-madder-800` | `oklch(36% 0.120 25)` | `#6f191a` |
| `--jx-madder-900` | `oklch(27% 0.088 25)` | `#480e0e` |
| `--jx-madder-950` | `oklch(21% 0.064 25)` | `#300908` |

</details>

## 3. Semantic roles

Light and dark values reference primitives.

### Surfaces and text

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-bg` | `paper-100` | `paper-950` | Page background |
| `--jx-surface` | `paper-50` | `paper-900` | Panels, lists, inputs, user chat messages |
| `--jx-surface-raised` | `paper-0` | `paper-800` | Popovers, toasts, menus (with shadow) |
| `--jx-surface-hover` | `paper-200` | `paper-800` | Hover fill on rows and ghost buttons |
| `--jx-surface-sunken` | `paper-200` | `paper-950` | Code blocks, wells, neutral badges |
| `--jx-fg` | `paper-900` | `paper-100` | Body text, titles |
| `--jx-fg-muted` | `paper-600` | `paper-400` | Secondary text, metadata, placeholders |
| `--jx-fg-subtle` | `paper-500` | `paper-500` | Disabled text, decorative meta, icons (3:1) |
| `--jx-border` | `paper-200` | `paper-800` | Dividers, panel edges (decorative) |
| `--jx-border-strong` | `paper-500` | `paper-500` | Input and control boundaries (3:1) |

### Accent and focus

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-accent` | `sage-700` | `sage-400` | Primary button fill, selected tab indicator |
| `--jx-accent-hover` | `sage-800` | `sage-300` | Primary hover |
| `--jx-accent-active` | `sage-900` | `sage-200` | Primary pressed |
| `--jx-accent-fg` | `paper-0` | `paper-950` | Text/icons on accent fills |
| `--jx-accent-text` | `sage-700` | `sage-300` | Links and accent-colored text |
| `--jx-accent-subtle` | `sage-100` | `sage-900` | Selected row / item fill |
| `--jx-focus` | `sage-600` | `sage-400` | Focus ring (2px, 2px offset) |
| `--jx-highlight` | `marigold-100` | `marigold-900` | Reading highlight (<mark>), markers |
| `--jx-highlight-strong` | `marigold-500` | `marigold-400` | Decorative marker dots, pending-write edge |

### Status

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-success` | `jade-700` | `jade-300` | Success text/icon |
| `--jx-success-bg` | `jade-50` | `jade-950` | Success banner/toast fill |
| `--jx-warn` | `persimmon-700` | `persimmon-300` | Warning text/icon |
| `--jx-warn-bg` | `persimmon-50` | `persimmon-950` | Warning banner fill (sync paused) |
| `--jx-danger` | `madder-700` | `madder-300` | Error text/icon |
| `--jx-danger-bg` | `madder-50` | `madder-950` | Error banner fill |
| `--jx-danger-solid` | `madder-700` | `madder-400` | Danger button fill |
| `--jx-danger-solid-fg` | `paper-0` | `paper-950` | Text on danger fill |

### Agent

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-ai` | `sage-50` | `sage-950` | Agent chat message fill (never KB content) |

### Labels

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-label-marigold-bg` | `marigold-100` | `marigold-900` | Label pill fill: marigold |
| `--jx-label-marigold-fg` | `marigold-800` | `marigold-200` | Label pill text: marigold |
| `--jx-label-plum-bg` | `plum-100` | `plum-900` | Label pill fill: plum |
| `--jx-label-plum-fg` | `plum-800` | `plum-200` | Label pill text: plum |
| `--jx-label-cornflower-bg` | `cornflower-100` | `cornflower-900` | Label pill fill: cornflower |
| `--jx-label-cornflower-fg` | `cornflower-800` | `cornflower-200` | Label pill text: cornflower |
| `--jx-label-clay-bg` | `clay-100` | `clay-900` | Label pill fill: clay |
| `--jx-label-clay-fg` | `clay-800` | `clay-200` | Label pill text: clay |
| `--jx-label-fern-bg` | `fern-100` | `fern-900` | Label pill fill: fern |
| `--jx-label-fern-fg` | `fern-800` | `fern-200` | Label pill text: fern |

### App identity

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-app-notes` | `sage-600` | `sage-400` | notes identity hue: tile glyph, PWA icon |
| `--jx-app-notes-chrome` | `sage-200` | `sage-800` | notes header band / chrome wash |
| `--jx-app-rss` | `marigold-600` | `marigold-400` | rss identity hue: tile glyph, PWA icon |
| `--jx-app-rss-chrome` | `marigold-200` | `marigold-800` | rss header band / chrome wash |
| `--jx-app-review` | `plum-600` | `plum-400` | review identity hue: tile glyph, PWA icon |
| `--jx-app-review-chrome` | `plum-200` | `plum-800` | review header band / chrome wash |
| `--jx-app-issues` | `cornflower-600` | `cornflower-400` | issues identity hue: tile glyph, PWA icon |
| `--jx-app-issues-chrome` | `cornflower-200` | `cornflower-800` | issues header band / chrome wash |

### Runner glyphs

| Token | Light | Dark | Use |
|---|---|---|---|
| `--jx-runner-claude` | `clay-700` | `clay-300` | claude monogram tile fill |
| `--jx-runner-claude-fg` | `paper-0` | `paper-950` | claude monogram letter |
| `--jx-runner-codex` | `fern-700` | `fern-300` | codex monogram tile fill |
| `--jx-runner-codex-fg` | `paper-0` | `paper-950` | codex monogram letter |

Notes:

- `--jx-ai` is only the agent's chat-message fill. It never marks KB content (brief §8).
- `--jx-app-<name>` is the identity hue for launcher tiles, glyphs, and the PWA icon.
  `--jx-app-<name>-chrome` is the header band or chrome wash (brief §10).
- The PWA manifest `theme_color` uses each app's `-chrome` value in the light theme:

  - notes: `#cae7d2`
  - rss: `#f6daab`
  - review: `#f8d1e9`
  - issues: `#cddfff`

## 4. Scales and other tokens

### Spacing, radius, shadow

| Token | Value |
|---|---|
| `--jx-space-1` | 4px |
| `--jx-space-2` | 8px |
| `--jx-space-3` | 12px |
| `--jx-space-4` | 16px |
| `--jx-space-5` | 24px |
| `--jx-space-6` | 32px |
| `--jx-space-7` | 48px |
| `--jx-space-8` | 64px |
| `--jx-radius-sm` | 4px |
| `--jx-radius-md` | 6px |
| `--jx-radius-lg` | 10px |
| `--jx-radius-full` | 999px |
| `--jx-shadow-1` (light) | `0 1px 2px oklch(25% 0.02 150 / 0.08), 0 0 0 1px oklch(25% 0.02 150 / 0.03)` |
| `--jx-shadow-1` (dark) | `0 1px 2px oklch(8% 0.01 150 / 0.50), 0 0 0 1px oklch(8% 0.01 150 / 0.30)` |
| `--jx-shadow-2` (light) | `0 6px 16px oklch(25% 0.02 150 / 0.10), 0 1px 3px oklch(25% 0.02 150 / 0.08)` |
| `--jx-shadow-2` (dark) | `0 8px 20px oklch(8% 0.01 150 / 0.55), 0 1px 3px oklch(8% 0.01 150 / 0.40)` |

Shadows are tinted with the neutral hue. They are themed tokens because dark mode needs
deeper, more opaque shadows to show any lift.

### Type

| Token | Size | Line height (`--jx-leading-*`) | Use |
|---|---|---|---|
| `--jx-text-xs` | 12px | 16px | Badges, captions |
| `--jx-text-sm` | 13px | 18px | Metadata, compact rows, buttons sm |
| `--jx-text-base` | 14px | 20px | UI default: rows, inputs, chat |
| `--jx-text-lg` | 16px | 24px | Panel titles, emphasized UI |
| `--jx-text-xl` | 20px | 28px | App/page titles |
| `--jx-text-2xl` | 26px | 32px | Launcher heading, wordmark |
| `--jx-text-read` | 17px | 27px | Reading body (Newsreader) |
| `--jx-text-read-lg` | 21px | 30px | Reading h2/h3 |
| `--jx-text-read-xl` | 30px | 36px | Reading h1 / article title |

| Token | Stack |
|---|---|
| `--jx-font-ui` | `"Bricolage Grotesque Variable", "Bricolage Grotesque", system-ui, -apple-system, sans-serif` |
| `--jx-font-read` | `"Newsreader Variable", "Newsreader", Charter, Georgia, serif` |
| `--jx-font-mono` | `"JetBrains Mono Variable", "JetBrains Mono", ui-monospace, Menlo, monospace` |

UI text sets `font-synthesis: none` (Bricolage has no italic) and
`font-optical-sizing: auto` everywhere. Ship `latin` and `latin-ext` subsets only.

### Weight, measure, focus, motion, density

| Token | Value | Note |
|---|---|---|
| `--jx-weight-regular` | `400` |  |
| `--jx-weight-medium` | `500` | UI emphasis (no italic in UI) |
| `--jx-weight-semibold` | `600` | Headings, selected row title; max in UI |
| `--jx-read-measure` | `66ch` | Reading column width |
| `--jx-focus-width` | `2px` |  |
| `--jx-focus-offset` | `2px` |  |
| `--jx-duration-fast` | `120ms` | Hover, press, toggles |
| `--jx-duration-base` | `200ms` | Panels, toasts, cards arriving |
| `--jx-ease` | `cubic-bezier(0.22, 1, 0.36, 1)` | Gentle ease-out; no springs |
| `--jx-row-pad-y` | `10px` | Desk density (RSS, Review, Notes) |
| `--jx-row-pad-x` | `14px` |  |
| `--jx-row-text` | `var(--jx-text-base)` |  |
| `--jx-row-leading` | `var(--jx-leading-base)` |  |

Compact density is set with `[data-density="compact"]` on a container. Issues uses it on its root:

| Token | Compact value |
|---|---|
| `--jx-row-pad-y` | `5px` |
| `--jx-row-pad-x` | `10px` |
| `--jx-row-text` | `var(--jx-text-sm)` |
| `--jx-row-leading` | `var(--jx-leading-sm)` |

Under `prefers-reduced-motion: reduce`, both durations become `0ms`. Components that fade
may keep an opacity change, but nothing moves.

### Component tokens

These are tier 3. Each one references a semantic role or a scale token. Task 4 specifies
how each component uses them.

<details><summary><b>Button</b></summary>

| Token | Value |
|---|---|
| `--jx-button-radius` | `var(--jx-radius-md)` |
| `--jx-button-height-sm` | `28px` |
| `--jx-button-height-md` | `32px` |
| `--jx-button-pad-x-sm` | `var(--jx-space-2)` |
| `--jx-button-pad-x-md` | `var(--jx-space-3)` |
| `--jx-button-text-sm` | `var(--jx-text-sm)` |
| `--jx-button-text-md` | `var(--jx-text-base)` |
| `--jx-button-weight` | `var(--jx-weight-medium)` |
| `--jx-button-primary-bg` | `var(--jx-accent)` |
| `--jx-button-primary-bg-hover` | `var(--jx-accent-hover)` |
| `--jx-button-primary-bg-active` | `var(--jx-accent-active)` |
| `--jx-button-primary-fg` | `var(--jx-accent-fg)` |
| `--jx-button-secondary-bg` | `var(--jx-surface)` |
| `--jx-button-secondary-bg-hover` | `var(--jx-surface-hover)` |
| `--jx-button-secondary-fg` | `var(--jx-fg)` |
| `--jx-button-secondary-border` | `var(--jx-border-strong)` |
| `--jx-button-ghost-bg-hover` | `var(--jx-surface-hover)` |
| `--jx-button-ghost-fg` | `var(--jx-fg)` |
| `--jx-button-danger-bg` | `var(--jx-danger-solid)` |
| `--jx-button-danger-fg` | `var(--jx-danger-solid-fg)` |
| `--jx-button-disabled-opacity` | `0.45` |

</details>

<details><summary><b>Input</b></summary>

| Token | Value |
|---|---|
| `--jx-input-height` | `32px` |
| `--jx-input-radius` | `var(--jx-radius-md)` |
| `--jx-input-pad-x` | `var(--jx-space-3)` |
| `--jx-input-bg` | `var(--jx-surface)` |
| `--jx-input-fg` | `var(--jx-fg)` |
| `--jx-input-placeholder` | `var(--jx-fg-muted)` |
| `--jx-input-border` | `var(--jx-border-strong)` |
| `--jx-input-border-focus` | `var(--jx-focus)` |
| `--jx-input-border-invalid` | `var(--jx-danger)` |
| `--jx-input-help-fg` | `var(--jx-fg-muted)` |
| `--jx-input-error-fg` | `var(--jx-danger)` |

</details>

<details><summary><b>Panel</b></summary>

| Token | Value |
|---|---|
| `--jx-panel-bg` | `var(--jx-surface)` |
| `--jx-panel-border` | `var(--jx-border)` |
| `--jx-panel-radius` | `var(--jx-radius-lg)` |
| `--jx-panel-pad` | `var(--jx-space-4)` |
| `--jx-panel-shadow` | `none` |
| `--jx-panel-raised-bg` | `var(--jx-surface-raised)` |
| `--jx-panel-raised-shadow` | `var(--jx-shadow-2)` |
| `--jx-panel-title-text` | `var(--jx-text-lg)` |
| `--jx-panel-title-weight` | `var(--jx-weight-semibold)` |

</details>

<details><summary><b>List/Row</b></summary>

| Token | Value |
|---|---|
| `--jx-row-bg` | `transparent` |
| `--jx-row-bg-hover` | `var(--jx-surface-hover)` |
| `--jx-row-bg-selected` | `var(--jx-accent-subtle)` |
| `--jx-row-title-weight-selected` | `var(--jx-weight-semibold)` |
| `--jx-row-divider` | `var(--jx-border)` |
| `--jx-row-meta-fg` | `var(--jx-fg-muted)` |
| `--jx-row-unread-dot` | `var(--jx-accent)` |

</details>

<details><summary><b>Badge</b></summary>

| Token | Value |
|---|---|
| `--jx-badge-radius` | `var(--jx-radius-full)` |
| `--jx-badge-pad-x` | `7px` |
| `--jx-badge-text` | `var(--jx-text-xs)` |
| `--jx-badge-neutral-bg` | `var(--jx-surface-sunken)` |
| `--jx-badge-neutral-fg` | `var(--jx-fg-muted)` |
| `--jx-badge-<label>-bg` | `var(--jx-label-<label>-bg)` |
| `--jx-badge-<label>-fg` | `var(--jx-label-<label>-fg)` |
| `--jx-badge-success-bg / -fg` | `var(--jx-success-bg) / var(--jx-success)` |
| `--jx-badge-warn-bg / -fg` | `var(--jx-warn-bg) / var(--jx-warn)` |
| `--jx-badge-danger-bg / -fg` | `var(--jx-danger-bg) / var(--jx-danger)` |

</details>

<details><summary><b>Tabs</b></summary>

| Token | Value |
|---|---|
| `--jx-tab-fg` | `var(--jx-fg-muted)` |
| `--jx-tab-fg-hover` | `var(--jx-fg)` |
| `--jx-tab-fg-selected` | `var(--jx-fg)` |
| `--jx-tab-indicator` | `var(--jx-accent)` |
| `--jx-tab-indicator-height` | `2px` |
| `--jx-tab-pad-x` | `var(--jx-space-3)` |
| `--jx-tab-height` | `36px` |
| `--jx-tab-weight-selected` | `var(--jx-weight-medium)` |

</details>

<details><summary><b>Toast</b></summary>

| Token | Value |
|---|---|
| `--jx-toast-bg` | `var(--jx-surface-raised)` |
| `--jx-toast-fg` | `var(--jx-fg)` |
| `--jx-toast-border` | `var(--jx-border)` |
| `--jx-toast-shadow` | `var(--jx-shadow-2)` |
| `--jx-toast-radius` | `var(--jx-radius-lg)` |
| `--jx-toast-width` | `360px` |
| `--jx-toast-icon-success` | `var(--jx-success)` |
| `--jx-toast-icon-warn` | `var(--jx-warn)` |
| `--jx-toast-icon-danger` | `var(--jx-danger)` |

</details>

<details><summary><b>ChatTranscript</b></summary>

| Token | Value |
|---|---|
| `--jx-chat-gap` | `var(--jx-space-3)` |
| `--jx-chat-bubble-radius` | `var(--jx-radius-lg)` |
| `--jx-chat-bubble-pad` | `var(--jx-space-2) var(--jx-space-3)` |
| `--jx-chat-bubble-user-bg` | `var(--jx-surface)` |
| `--jx-chat-bubble-user-border` | `var(--jx-border)` |
| `--jx-chat-bubble-agent-bg` | `var(--jx-ai)` |
| `--jx-chat-bubble-agent-fg` | `var(--jx-fg)` |
| `--jx-chat-meta-fg` | `var(--jx-fg-muted)` |
| `--jx-chat-tool-fg` | `var(--jx-fg-muted)` |
| `--jx-chat-tool-font` | `var(--jx-font-mono)` |
| `--jx-chat-error-bg` | `var(--jx-danger-bg)` |
| `--jx-chat-error-fg` | `var(--jx-danger)` |
| `--jx-chat-pending-bg` | `var(--jx-surface-raised)` |
| `--jx-chat-pending-border` | `var(--jx-highlight-strong)` |
| `--jx-chat-pending-shadow` | `var(--jx-shadow-1)` |
| `--jx-chat-cursor` | `var(--jx-accent)` |
| `--jx-runner-glyph-size` | `20px` |
| `--jx-runner-glyph-radius` | `var(--jx-radius-sm)` |

</details>

<details><summary><b>Drag and drop</b> (components.md §11)</summary>

| Token | Value |
|---|---|
| `--jx-dnd-dragging-opacity` | `var(--jx-button-disabled-opacity)` |
| `--jx-dnd-unavailable-opacity` | `0.6` |
| `--jx-dnd-line` | `var(--jx-accent)` |
| `--jx-dnd-line-width` | `2px` |
| `--jx-dnd-preview-max-width` | `320px` |
| `--jx-dnd-empty-min-height` | `44px` (a touch target) |

</details>

### App-overridable tokens

Each app sets `data-app="<name>"` on its root, which defines `--jx-app` and
`--jx-app-chrome`. Within its own root an app **may** override:

- `--jx-app`, `--jx-app-chrome`, and any `--jx-app-*` tokens it adds itself (washes,
  bands, illustrations)
- `--jx-radius-*`, `--jx-row-*` (density), `--jx-panel-*` (except colors that carry
  text), and `--jx-text-read*` / `--jx-read-measure`
- New app-local tokens, prefixed `--jx-<app>-*` (for example `--jx-review-quiz-*`)

An app **may not** override: semantic color roles (`--jx-bg` … `--jx-ai`), the accent,
focus, or status tokens, the label set, font stacks, motion tokens, or ChatTranscript
tokens. Any new text/background pair an app introduces must be added to its own contrast
fixture, which Task 22's test runs alongside the core table.

## 5. Contrast table

Computed WCAG 2.x ratios (sRGB relative luminance, from the oklch values above).
Minimums are 4.5:1 for text and 3:1 for UI boundaries, icons, and focus indicators. **Every pair
passes in both themes.** Task 22 parses this table as its fixture, so keep its columns
stable.

| Foreground | Background | Min | Light | Dark | Use |
|---|---|---|---|---|---|
| `--jx-fg` | `--jx-bg` | 4.5 | 15.39 | 16.90 | Body text on page |
| `--jx-fg` | `--jx-surface` | 4.5 | 16.25 | 15.39 | Body text on panels |
| `--jx-fg` | `--jx-surface-raised` | 4.5 | 16.52 | 13.01 | Text in popovers/toasts |
| `--jx-fg` | `--jx-surface-hover` | 4.5 | 13.75 | 13.01 | Hovered row text |
| `--jx-fg` | `--jx-surface-sunken` | 4.5 | 13.75 | 16.90 | Code block text |
| `--jx-fg-muted` | `--jx-bg` | 4.5 | 5.77 | 9.15 | Metadata on page |
| `--jx-fg-muted` | `--jx-surface` | 4.5 | 6.09 | 8.33 | Metadata, placeholders |
| `--jx-fg-muted` | `--jx-surface-raised` | 4.5 | 6.19 | 7.05 | Metadata in popovers |
| `--jx-fg-muted` | `--jx-surface-sunken` | 4.5 | 5.16 | 9.15 | Neutral badge text |
| `--jx-fg-muted` | `--jx-surface-hover` | 4.5 | 5.16 | 7.05 | Hovered row metadata |
| `--jx-fg-subtle` | `--jx-surface` | 3 | 3.84 | 4.23 | Icons, disabled (UI 3:1) |
| `--jx-fg-subtle` | `--jx-bg` | 3 | 3.64 | 4.65 | Icons on page (UI 3:1) |
| `--jx-border-strong` | `--jx-surface` | 3 | 3.84 | 4.23 | Input boundary |
| `--jx-border-strong` | `--jx-bg` | 3 | 3.64 | 4.65 | Control boundary on page |
| `--jx-accent-fg` | `--jx-accent` | 4.5 | 7.11 | 8.18 | Primary button label |
| `--jx-accent-fg` | `--jx-accent-hover` | 4.5 | 10.47 | 11.07 | Primary hover label |
| `--jx-accent-fg` | `--jx-accent-active` | 4.5 | 14.72 | 13.74 | Primary pressed label |
| `--jx-accent` | `--jx-bg` | 3 | 6.62 | 8.18 | Tab indicator, primary edge |
| `--jx-accent` | `--jx-surface` | 3 | 6.99 | 7.45 | Tab indicator on panel |
| `--jx-accent-text` | `--jx-bg` | 4.5 | 6.62 | 11.07 | Link on page |
| `--jx-accent-text` | `--jx-surface` | 4.5 | 6.99 | 10.08 | Link on panel |
| `--jx-accent-text` | `--jx-ai` | 4.5 | 6.68 | 10.65 | Link inside agent message |
| `--jx-fg` | `--jx-accent-subtle` | 4.5 | 14.47 | 13.71 | Selected row title |
| `--jx-fg-muted` | `--jx-accent-subtle` | 4.5 | 5.42 | 7.42 | Selected row metadata |
| `--jx-focus` | `--jx-bg` | 3 | 4.49 | 8.18 | Focus ring on page |
| `--jx-focus` | `--jx-surface` | 3 | 4.74 | 7.45 | Focus ring on panel |
| `--jx-fg` | `--jx-ai` | 4.5 | 15.52 | 16.26 | Agent message text |
| `--jx-fg-muted` | `--jx-ai` | 4.5 | 5.82 | 8.81 | Agent message metadata |
| `--jx-fg` | `--jx-highlight` | 4.5 | 14.33 | 14.04 | Highlighted reading text |
| `--jx-success` | `--jx-surface` | 4.5 | 6.98 | 10.11 | Success text |
| `--jx-success` | `--jx-success-bg` | 4.5 | 6.67 | 10.67 | Success banner text |
| `--jx-fg` | `--jx-success-bg` | 4.5 | 15.53 | 16.25 | Body text in success banner |
| `--jx-warn` | `--jx-surface` | 4.5 | 7.57 | 9.59 | Warning text |
| `--jx-warn` | `--jx-warn-bg` | 4.5 | 7.18 | 10.32 | Warning banner text |
| `--jx-fg` | `--jx-warn-bg` | 4.5 | 15.41 | 16.57 | Body text in warning banner |
| `--jx-danger` | `--jx-surface` | 4.5 | 7.84 | 9.49 | Error text |
| `--jx-danger` | `--jx-danger-bg` | 4.5 | 7.43 | 10.29 | Error banner text |
| `--jx-fg` | `--jx-danger-bg` | 4.5 | 15.39 | 16.69 | Body text in error banner |
| `--jx-danger-solid-fg` | `--jx-danger-solid` | 4.5 | 7.97 | 7.44 | Danger button label |
| `--jx-success` | `--jx-surface-raised` | 4.5 | 7.10 | 8.55 | Toast success icon/text |
| `--jx-warn` | `--jx-surface-raised` | 4.5 | 7.70 | 8.11 | Toast warning icon/text |
| `--jx-danger` | `--jx-surface-raised` | 4.5 | 7.97 | 8.03 | Toast error icon/text |
| `--jx-label-marigold-fg` | `--jx-label-marigold-bg` | 4.5 | 9.46 | 11.20 | Label pill: marigold |
| `--jx-label-plum-fg` | `--jx-label-plum-bg` | 4.5 | 9.73 | 11.24 | Label pill: plum |
| `--jx-label-cornflower-fg` | `--jx-label-cornflower-bg` | 4.5 | 9.48 | 11.24 | Label pill: cornflower |
| `--jx-label-clay-fg` | `--jx-label-clay-bg` | 4.5 | 9.52 | 11.21 | Label pill: clay |
| `--jx-label-fern-fg` | `--jx-label-fern-bg` | 4.5 | 9.23 | 11.17 | Label pill: fern |
| `--jx-app-notes` | `--jx-surface` | 3 | 4.74 | 7.45 | notes tile glyph (UI 3:1) |
| `--jx-fg` | `--jx-app-notes-chrome` | 4.5 | 12.51 | 9.75 | Text on notes chrome |
| `--jx-fg-muted` | `--jx-app-notes-chrome` | 4.5 | 4.69 | 5.28 | Metadata on notes chrome |
| `--jx-accent-text` | `--jx-app-notes-chrome` | 4.5 | 5.38 | 6.39 | Link/active nav on notes chrome |
| `--jx-app-rss` | `--jx-surface` | 3 | 5.02 | 7.10 | rss tile glyph (UI 3:1) |
| `--jx-fg` | `--jx-app-rss-chrome` | 4.5 | 12.27 | 10.16 | Text on rss chrome |
| `--jx-fg-muted` | `--jx-app-rss-chrome` | 4.5 | 4.60 | 5.50 | Metadata on rss chrome |
| `--jx-accent-text` | `--jx-app-rss-chrome` | 4.5 | 5.28 | 6.66 | Link/active nav on rss chrome |
| `--jx-app-review` | `--jx-surface` | 3 | 5.29 | 6.85 | review tile glyph (UI 3:1) |
| `--jx-fg` | `--jx-app-review-chrome` | 4.5 | 12.06 | 10.54 | Text on review chrome |
| `--jx-fg-muted` | `--jx-app-review-chrome` | 4.5 | 4.52 | 5.71 | Metadata on review chrome |
| `--jx-accent-text` | `--jx-app-review-chrome` | 4.5 | 5.19 | 6.91 | Link/active nav on review chrome |
| `--jx-app-issues` | `--jx-surface` | 3 | 5.01 | 7.16 | issues tile glyph (UI 3:1) |
| `--jx-fg` | `--jx-app-issues-chrome` | 4.5 | 12.32 | 10.16 | Text on issues chrome |
| `--jx-fg-muted` | `--jx-app-issues-chrome` | 4.5 | 4.62 | 5.50 | Metadata on issues chrome |
| `--jx-accent-text` | `--jx-app-issues-chrome` | 4.5 | 5.30 | 6.66 | Link/active nav on issues chrome |
| `--jx-runner-claude-fg` | `--jx-runner-claude` | 4.5 | 7.58 | 10.66 | claude monogram |
| `--jx-runner-codex-fg` | `--jx-runner-codex` | 4.5 | 7.17 | 11.03 | codex monogram |

Decorative pairs are deliberately excluded: `--jx-border` dividers, the `--jx-ai` fill
against the surface, the `--jx-highlight` fill against the page, and app chrome against
the page. None of them carry information on their own (WCAG 1.4.11 exempts them).

## 6. Full CSS

The complete token stylesheet, as `@jesinia/design` will ship it in `tokens.css`:

```css
/* Tier 1: primitives (theme-independent) */
:root {
  --jx-paper-0: oklch(99.8% 0.002 120);
  --jx-paper-50: oklch(99.2% 0.004 122);
  --jx-paper-100: oklch(97.3% 0.007 123);
  --jx-paper-200: oklch(93.5% 0.010 126);
  --jx-paper-300: oklch(88.5% 0.012 129);
  --jx-paper-400: oklch(78% 0.013 133);
  --jx-paper-500: oklch(60% 0.014 136);
  --jx-paper-600: oklch(49% 0.014 139);
  --jx-paper-700: oklch(38% 0.014 142);
  --jx-paper-800: oklch(29% 0.013 145);
  --jx-paper-900: oklch(23.5% 0.013 148);
  --jx-paper-950: oklch(19.5% 0.012 150);
  --jx-scrim: oklch(19.5% 0.012 150 / 0.25); /* --jx-paper-950 at 25%: modal scrim only */
  --jx-sage-50: oklch(97.5% 0.013 155);
  --jx-sage-100: oklch(95% 0.022 155);
  --jx-sage-200: oklch(90% 0.041 155);
  --jx-sage-300: oklch(83% 0.063 155);
  --jx-sage-400: oklch(74% 0.081 155);
  --jx-sage-500: oklch(64% 0.090 155);
  --jx-sage-600: oklch(54% 0.090 155);
  --jx-sage-700: oklch(45% 0.081 155);
  --jx-sage-800: oklch(36% 0.068 155);
  --jx-sage-900: oklch(27% 0.050 155);
  --jx-sage-950: oklch(21% 0.036 155);
  --jx-marigold-50: oklch(97.5% 0.022 80);
  --jx-marigold-100: oklch(95% 0.037 80);
  --jx-marigold-200: oklch(90% 0.068 80);
  --jx-marigold-300: oklch(83% 0.105 80);
  --jx-marigold-400: oklch(74% 0.135 80);
  --jx-marigold-500: oklch(64% 0.132 80);
  --jx-marigold-600: oklch(54% 0.111 80);
  --jx-marigold-700: oklch(45% 0.093 80);
  --jx-marigold-800: oklch(36% 0.074 80);
  --jx-marigold-900: oklch(27% 0.056 80);
  --jx-marigold-950: oklch(21% 0.044 80);
  --jx-plum-50: oklch(97.5% 0.016 340);
  --jx-plum-100: oklch(95% 0.030 340);
  --jx-plum-200: oklch(90% 0.054 340);
  --jx-plum-300: oklch(83% 0.084 340);
  --jx-plum-400: oklch(74% 0.108 340);
  --jx-plum-500: oklch(64% 0.120 340);
  --jx-plum-600: oklch(54% 0.120 340);
  --jx-plum-700: oklch(45% 0.108 340);
  --jx-plum-800: oklch(36% 0.090 340);
  --jx-plum-900: oklch(27% 0.066 340);
  --jx-plum-950: oklch(21% 0.048 340);
  --jx-cornflower-50: oklch(97.5% 0.011 262);
  --jx-cornflower-100: oklch(95% 0.023 262);
  --jx-cornflower-200: oklch(90% 0.048 262);
  --jx-cornflower-300: oklch(83% 0.084 262);
  --jx-cornflower-400: oklch(74% 0.108 262);
  --jx-cornflower-500: oklch(64% 0.120 262);
  --jx-cornflower-600: oklch(54% 0.120 262);
  --jx-cornflower-700: oklch(45% 0.108 262);
  --jx-cornflower-800: oklch(36% 0.090 262);
  --jx-cornflower-900: oklch(27% 0.066 262);
  --jx-cornflower-950: oklch(21% 0.048 262);
  --jx-clay-50: oklch(97.5% 0.011 52);
  --jx-clay-100: oklch(95% 0.019 52);
  --jx-clay-200: oklch(90% 0.034 52);
  --jx-clay-300: oklch(83% 0.052 52);
  --jx-clay-400: oklch(74% 0.068 52);
  --jx-clay-500: oklch(64% 0.075 52);
  --jx-clay-600: oklch(54% 0.075 52);
  --jx-clay-700: oklch(45% 0.068 52);
  --jx-clay-800: oklch(36% 0.056 52);
  --jx-clay-900: oklch(27% 0.041 52);
  --jx-clay-950: oklch(21% 0.030 52);
  --jx-fern-50: oklch(97.5% 0.017 128);
  --jx-fern-100: oklch(95% 0.028 128);
  --jx-fern-200: oklch(90% 0.050 128);
  --jx-fern-300: oklch(83% 0.077 128);
  --jx-fern-400: oklch(74% 0.099 128);
  --jx-fern-500: oklch(64% 0.110 128);
  --jx-fern-600: oklch(54% 0.110 128);
  --jx-fern-700: oklch(45% 0.099 128);
  --jx-fern-800: oklch(36% 0.083 128);
  --jx-fern-900: oklch(27% 0.061 128);
  --jx-fern-950: oklch(21% 0.044 128);
  --jx-jade-50: oklch(97.5% 0.014 185);
  --jx-jade-100: oklch(95% 0.024 185);
  --jx-jade-200: oklch(90% 0.043 185);
  --jx-jade-300: oklch(83% 0.066 185);
  --jx-jade-400: oklch(74% 0.086 185);
  --jx-jade-500: oklch(64% 0.095 185);
  --jx-jade-600: oklch(54% 0.095 185);
  --jx-jade-700: oklch(45% 0.079 185);
  --jx-jade-800: oklch(36% 0.063 185);
  --jx-jade-900: oklch(27% 0.047 185);
  --jx-jade-950: oklch(21% 0.037 185);
  --jx-persimmon-50: oklch(97.5% 0.014 55);
  --jx-persimmon-100: oklch(95% 0.029 55);
  --jx-persimmon-200: oklch(90% 0.061 55);
  --jx-persimmon-300: oklch(83% 0.105 55);
  --jx-persimmon-400: oklch(74% 0.135 55);
  --jx-persimmon-500: oklch(64% 0.150 55);
  --jx-persimmon-600: oklch(54% 0.133 55);
  --jx-persimmon-700: oklch(45% 0.111 55);
  --jx-persimmon-800: oklch(36% 0.089 55);
  --jx-persimmon-900: oklch(27% 0.067 55);
  --jx-persimmon-950: oklch(21% 0.052 55);
  --jx-madder-50: oklch(97.5% 0.012 25);
  --jx-madder-100: oklch(95% 0.024 25);
  --jx-madder-200: oklch(90% 0.051 25);
  --jx-madder-300: oklch(83% 0.094 25);
  --jx-madder-400: oklch(74% 0.144 25);
  --jx-madder-500: oklch(64% 0.160 25);
  --jx-madder-600: oklch(54% 0.160 25);
  --jx-madder-700: oklch(45% 0.144 25);
  --jx-madder-800: oklch(36% 0.120 25);
  --jx-madder-900: oklch(27% 0.088 25);
  --jx-madder-950: oklch(21% 0.064 25);
}

/* Scales (theme-independent) */
:root {
  --jx-space-1: 4px;
  --jx-space-2: 8px;
  --jx-space-3: 12px;
  --jx-space-4: 16px;
  --jx-space-5: 24px;
  --jx-space-6: 32px;
  --jx-space-7: 48px;
  --jx-space-8: 64px;
  --jx-radius-sm: 4px;
  --jx-radius-md: 6px;
  --jx-radius-lg: 10px;
  --jx-radius-full: 999px;
  --jx-text-xs: 12px;
  --jx-leading-xs: 16px;
  --jx-text-sm: 13px;
  --jx-leading-sm: 18px;
  --jx-text-base: 14px;
  --jx-leading-base: 20px;
  --jx-text-lg: 16px;
  --jx-leading-lg: 24px;
  --jx-text-xl: 20px;
  --jx-leading-xl: 28px;
  --jx-text-2xl: 26px;
  --jx-leading-2xl: 32px;
  --jx-text-read: 17px;
  --jx-leading-read: 27px;
  --jx-text-read-lg: 21px;
  --jx-leading-read-lg: 30px;
  --jx-text-read-xl: 30px;
  --jx-leading-read-xl: 36px;
  --jx-font-ui: "Bricolage Grotesque Variable", "Bricolage Grotesque", system-ui, -apple-system, sans-serif;
  --jx-font-read: "Newsreader Variable", "Newsreader", Charter, Georgia, serif;
  --jx-font-mono: "JetBrains Mono Variable", "JetBrains Mono", ui-monospace, Menlo, monospace;
  --jx-weight-regular: 400;
  --jx-weight-medium: 500;
  --jx-weight-semibold: 600;
  --jx-read-measure: 66ch;
  --jx-focus-width: 2px;
  --jx-focus-offset: 2px;
  --jx-duration-fast: 120ms;
  --jx-duration-base: 200ms;
  --jx-ease: cubic-bezier(0.22, 1, 0.36, 1);
  --jx-row-pad-y: 10px;
  --jx-row-pad-x: 14px;
  --jx-row-text: var(--jx-text-base);
  --jx-row-leading: var(--jx-leading-base);
}

/* Tier 2: semantic roles, light (default) */
:root,
[data-theme="light"] {
  color-scheme: light;
  --jx-bg: var(--jx-paper-100);
  --jx-surface: var(--jx-paper-50);
  --jx-surface-raised: var(--jx-paper-0);
  --jx-surface-hover: var(--jx-paper-200);
  --jx-surface-sunken: var(--jx-paper-200);
  --jx-fg: var(--jx-paper-900);
  --jx-fg-muted: var(--jx-paper-600);
  --jx-fg-subtle: var(--jx-paper-500);
  --jx-border: var(--jx-paper-200);
  --jx-border-strong: var(--jx-paper-500);
  --jx-accent: var(--jx-sage-700);
  --jx-accent-hover: var(--jx-sage-800);
  --jx-accent-active: var(--jx-sage-900);
  --jx-accent-fg: var(--jx-paper-0);
  --jx-accent-text: var(--jx-sage-700);
  --jx-accent-subtle: var(--jx-sage-100);
  --jx-focus: var(--jx-sage-600);
  --jx-highlight: var(--jx-marigold-100);
  --jx-highlight-strong: var(--jx-marigold-500);
  --jx-success: var(--jx-jade-700);
  --jx-success-bg: var(--jx-jade-50);
  --jx-warn: var(--jx-persimmon-700);
  --jx-warn-bg: var(--jx-persimmon-50);
  --jx-danger: var(--jx-madder-700);
  --jx-danger-bg: var(--jx-madder-50);
  --jx-danger-solid: var(--jx-madder-700);
  --jx-danger-solid-fg: var(--jx-paper-0);
  --jx-ai: var(--jx-sage-50);
  --jx-label-marigold-bg: var(--jx-marigold-100);
  --jx-label-marigold-fg: var(--jx-marigold-800);
  --jx-label-plum-bg: var(--jx-plum-100);
  --jx-label-plum-fg: var(--jx-plum-800);
  --jx-label-cornflower-bg: var(--jx-cornflower-100);
  --jx-label-cornflower-fg: var(--jx-cornflower-800);
  --jx-label-clay-bg: var(--jx-clay-100);
  --jx-label-clay-fg: var(--jx-clay-800);
  --jx-label-fern-bg: var(--jx-fern-100);
  --jx-label-fern-fg: var(--jx-fern-800);
  --jx-app-notes: var(--jx-sage-600);
  --jx-app-notes-chrome: var(--jx-sage-200);
  --jx-app-rss: var(--jx-marigold-600);
  --jx-app-rss-chrome: var(--jx-marigold-200);
  --jx-app-review: var(--jx-plum-600);
  --jx-app-review-chrome: var(--jx-plum-200);
  --jx-app-issues: var(--jx-cornflower-600);
  --jx-app-issues-chrome: var(--jx-cornflower-200);
  --jx-runner-claude: var(--jx-clay-700);
  --jx-runner-claude-fg: var(--jx-paper-0);
  --jx-runner-codex: var(--jx-fern-700);
  --jx-runner-codex-fg: var(--jx-paper-0);
  --jx-shadow-1: 0 1px 2px oklch(25% 0.02 150 / 0.08), 0 0 0 1px oklch(25% 0.02 150 / 0.03);
  --jx-shadow-2: 0 6px 16px oklch(25% 0.02 150 / 0.10), 0 1px 3px oklch(25% 0.02 150 / 0.08);
}

/* Tier 2: semantic roles, dark */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --jx-bg: var(--jx-paper-950);
    --jx-surface: var(--jx-paper-900);
    --jx-surface-raised: var(--jx-paper-800);
    --jx-surface-hover: var(--jx-paper-800);
    --jx-surface-sunken: var(--jx-paper-950);
    --jx-fg: var(--jx-paper-100);
    --jx-fg-muted: var(--jx-paper-400);
    --jx-fg-subtle: var(--jx-paper-500);
    --jx-border: var(--jx-paper-800);
    --jx-border-strong: var(--jx-paper-500);
    --jx-accent: var(--jx-sage-400);
    --jx-accent-hover: var(--jx-sage-300);
    --jx-accent-active: var(--jx-sage-200);
    --jx-accent-fg: var(--jx-paper-950);
    --jx-accent-text: var(--jx-sage-300);
    --jx-accent-subtle: var(--jx-sage-900);
    --jx-focus: var(--jx-sage-400);
    --jx-highlight: var(--jx-marigold-900);
    --jx-highlight-strong: var(--jx-marigold-400);
    --jx-success: var(--jx-jade-300);
    --jx-success-bg: var(--jx-jade-950);
    --jx-warn: var(--jx-persimmon-300);
    --jx-warn-bg: var(--jx-persimmon-950);
    --jx-danger: var(--jx-madder-300);
    --jx-danger-bg: var(--jx-madder-950);
    --jx-danger-solid: var(--jx-madder-400);
    --jx-danger-solid-fg: var(--jx-paper-950);
    --jx-ai: var(--jx-sage-950);
    --jx-label-marigold-bg: var(--jx-marigold-900);
    --jx-label-marigold-fg: var(--jx-marigold-200);
    --jx-label-plum-bg: var(--jx-plum-900);
    --jx-label-plum-fg: var(--jx-plum-200);
    --jx-label-cornflower-bg: var(--jx-cornflower-900);
    --jx-label-cornflower-fg: var(--jx-cornflower-200);
    --jx-label-clay-bg: var(--jx-clay-900);
    --jx-label-clay-fg: var(--jx-clay-200);
    --jx-label-fern-bg: var(--jx-fern-900);
    --jx-label-fern-fg: var(--jx-fern-200);
    --jx-app-notes: var(--jx-sage-400);
    --jx-app-notes-chrome: var(--jx-sage-800);
    --jx-app-rss: var(--jx-marigold-400);
    --jx-app-rss-chrome: var(--jx-marigold-800);
    --jx-app-review: var(--jx-plum-400);
    --jx-app-review-chrome: var(--jx-plum-800);
    --jx-app-issues: var(--jx-cornflower-400);
    --jx-app-issues-chrome: var(--jx-cornflower-800);
    --jx-runner-claude: var(--jx-clay-300);
    --jx-runner-claude-fg: var(--jx-paper-950);
    --jx-runner-codex: var(--jx-fern-300);
    --jx-runner-codex-fg: var(--jx-paper-950);
    --jx-shadow-1: 0 1px 2px oklch(8% 0.01 150 / 0.50), 0 0 0 1px oklch(8% 0.01 150 / 0.30);
    --jx-shadow-2: 0 8px 20px oklch(8% 0.01 150 / 0.55), 0 1px 3px oklch(8% 0.01 150 / 0.40);
  }
}
[data-theme="dark"] {
  color-scheme: dark;
  --jx-bg: var(--jx-paper-950);
  --jx-surface: var(--jx-paper-900);
  --jx-surface-raised: var(--jx-paper-800);
  --jx-surface-hover: var(--jx-paper-800);
  --jx-surface-sunken: var(--jx-paper-950);
  --jx-fg: var(--jx-paper-100);
  --jx-fg-muted: var(--jx-paper-400);
  --jx-fg-subtle: var(--jx-paper-500);
  --jx-border: var(--jx-paper-800);
  --jx-border-strong: var(--jx-paper-500);
  --jx-accent: var(--jx-sage-400);
  --jx-accent-hover: var(--jx-sage-300);
  --jx-accent-active: var(--jx-sage-200);
  --jx-accent-fg: var(--jx-paper-950);
  --jx-accent-text: var(--jx-sage-300);
  --jx-accent-subtle: var(--jx-sage-900);
  --jx-focus: var(--jx-sage-400);
  --jx-highlight: var(--jx-marigold-900);
  --jx-highlight-strong: var(--jx-marigold-400);
  --jx-success: var(--jx-jade-300);
  --jx-success-bg: var(--jx-jade-950);
  --jx-warn: var(--jx-persimmon-300);
  --jx-warn-bg: var(--jx-persimmon-950);
  --jx-danger: var(--jx-madder-300);
  --jx-danger-bg: var(--jx-madder-950);
  --jx-danger-solid: var(--jx-madder-400);
  --jx-danger-solid-fg: var(--jx-paper-950);
  --jx-ai: var(--jx-sage-950);
  --jx-label-marigold-bg: var(--jx-marigold-900);
  --jx-label-marigold-fg: var(--jx-marigold-200);
  --jx-label-plum-bg: var(--jx-plum-900);
  --jx-label-plum-fg: var(--jx-plum-200);
  --jx-label-cornflower-bg: var(--jx-cornflower-900);
  --jx-label-cornflower-fg: var(--jx-cornflower-200);
  --jx-label-clay-bg: var(--jx-clay-900);
  --jx-label-clay-fg: var(--jx-clay-200);
  --jx-label-fern-bg: var(--jx-fern-900);
  --jx-label-fern-fg: var(--jx-fern-200);
  --jx-app-notes: var(--jx-sage-400);
  --jx-app-notes-chrome: var(--jx-sage-800);
  --jx-app-rss: var(--jx-marigold-400);
  --jx-app-rss-chrome: var(--jx-marigold-800);
  --jx-app-review: var(--jx-plum-400);
  --jx-app-review-chrome: var(--jx-plum-800);
  --jx-app-issues: var(--jx-cornflower-400);
  --jx-app-issues-chrome: var(--jx-cornflower-800);
  --jx-runner-claude: var(--jx-clay-300);
  --jx-runner-claude-fg: var(--jx-paper-950);
  --jx-runner-codex: var(--jx-fern-300);
  --jx-runner-codex-fg: var(--jx-paper-950);
  --jx-shadow-1: 0 1px 2px oklch(8% 0.01 150 / 0.50), 0 0 0 1px oklch(8% 0.01 150 / 0.30);
  --jx-shadow-2: 0 8px 20px oklch(8% 0.01 150 / 0.55), 0 1px 3px oklch(8% 0.01 150 / 0.40);
}

/* Tier 3: component tokens (reference semantic roles only).
   Also declared on [data-theme] so they re-resolve inside a themed subtree. */
:root,
[data-theme] {
  --jx-button-radius: var(--jx-radius-md);
  --jx-button-height-sm: 28px;
  --jx-button-height-md: 32px;
  --jx-button-pad-x-sm: var(--jx-space-2);
  --jx-button-pad-x-md: var(--jx-space-3);
  --jx-button-text-sm: var(--jx-text-sm);
  --jx-button-text-md: var(--jx-text-base);
  --jx-button-weight: var(--jx-weight-medium);
  --jx-button-primary-bg: var(--jx-accent);
  --jx-button-primary-bg-hover: var(--jx-accent-hover);
  --jx-button-primary-bg-active: var(--jx-accent-active);
  --jx-button-primary-fg: var(--jx-accent-fg);
  --jx-button-secondary-bg: var(--jx-surface);
  --jx-button-secondary-bg-hover: var(--jx-surface-hover);
  --jx-button-secondary-fg: var(--jx-fg);
  --jx-button-secondary-border: var(--jx-border-strong);
  --jx-button-ghost-bg-hover: var(--jx-surface-hover);
  --jx-button-ghost-fg: var(--jx-fg);
  --jx-button-danger-bg: var(--jx-danger-solid);
  --jx-button-danger-fg: var(--jx-danger-solid-fg);
  --jx-button-disabled-opacity: 0.45;
  --jx-input-height: 32px;
  --jx-input-radius: var(--jx-radius-md);
  --jx-input-pad-x: var(--jx-space-3);
  --jx-input-bg: var(--jx-surface);
  --jx-input-fg: var(--jx-fg);
  --jx-input-placeholder: var(--jx-fg-muted);
  --jx-input-border: var(--jx-border-strong);
  --jx-input-border-focus: var(--jx-focus);
  --jx-input-border-invalid: var(--jx-danger);
  --jx-input-help-fg: var(--jx-fg-muted);
  --jx-input-error-fg: var(--jx-danger);
  --jx-panel-bg: var(--jx-surface);
  --jx-panel-border: var(--jx-border);
  --jx-panel-radius: var(--jx-radius-lg);
  --jx-panel-pad: var(--jx-space-4);
  --jx-panel-shadow: none;
  --jx-panel-raised-bg: var(--jx-surface-raised);
  --jx-panel-raised-shadow: var(--jx-shadow-2);
  --jx-panel-title-text: var(--jx-text-lg);
  --jx-panel-title-weight: var(--jx-weight-semibold);
  --jx-row-bg: transparent;
  --jx-row-bg-hover: var(--jx-surface-hover);
  --jx-row-bg-selected: var(--jx-accent-subtle);
  --jx-row-title-weight-selected: var(--jx-weight-semibold);
  --jx-row-divider: var(--jx-border);
  --jx-row-meta-fg: var(--jx-fg-muted);
  --jx-row-unread-dot: var(--jx-accent);
  --jx-badge-radius: var(--jx-radius-full);
  --jx-badge-pad-x: 7px;
  --jx-badge-text: var(--jx-text-xs);
  --jx-badge-neutral-bg: var(--jx-surface-sunken);
  --jx-badge-neutral-fg: var(--jx-fg-muted);
  --jx-tab-fg: var(--jx-fg-muted);
  --jx-tab-fg-hover: var(--jx-fg);
  --jx-tab-fg-selected: var(--jx-fg);
  --jx-tab-indicator: var(--jx-accent);
  --jx-tab-indicator-height: 2px;
  --jx-tab-pad-x: var(--jx-space-3);
  --jx-tab-height: 36px;
  --jx-tab-weight-selected: var(--jx-weight-medium);
  --jx-toast-bg: var(--jx-surface-raised);
  --jx-toast-fg: var(--jx-fg);
  --jx-toast-border: var(--jx-border);
  --jx-toast-shadow: var(--jx-shadow-2);
  --jx-toast-radius: var(--jx-radius-lg);
  --jx-toast-width: 360px;
  --jx-toast-icon-success: var(--jx-success);
  --jx-toast-icon-warn: var(--jx-warn);
  --jx-toast-icon-danger: var(--jx-danger);
  --jx-chat-gap: var(--jx-space-3);
  --jx-chat-bubble-radius: var(--jx-radius-lg);
  --jx-chat-bubble-pad: var(--jx-space-2) var(--jx-space-3);
  --jx-chat-bubble-user-bg: var(--jx-surface);
  --jx-chat-bubble-user-border: var(--jx-border);
  --jx-chat-bubble-agent-bg: var(--jx-ai);
  --jx-chat-bubble-agent-fg: var(--jx-fg);
  --jx-chat-meta-fg: var(--jx-fg-muted);
  --jx-chat-tool-fg: var(--jx-fg-muted);
  --jx-chat-tool-font: var(--jx-font-mono);
  --jx-chat-error-bg: var(--jx-danger-bg);
  --jx-chat-error-fg: var(--jx-danger);
  --jx-chat-pending-bg: var(--jx-surface-raised);
  --jx-chat-pending-border: var(--jx-highlight-strong);
  --jx-chat-pending-shadow: var(--jx-shadow-1);
  --jx-chat-cursor: var(--jx-accent);
  --jx-runner-glyph-size: 20px;
  --jx-runner-glyph-radius: var(--jx-radius-sm);
  --jx-dnd-dragging-opacity: var(--jx-button-disabled-opacity);
  --jx-dnd-unavailable-opacity: 0.6;
  --jx-dnd-line: var(--jx-accent);
  --jx-dnd-line-width: 2px;
  --jx-dnd-preview-max-width: 320px;
  --jx-dnd-empty-min-height: 44px;
  --jx-badge-marigold-bg: var(--jx-label-marigold-bg);
  --jx-badge-marigold-fg: var(--jx-label-marigold-fg);
  --jx-badge-plum-bg: var(--jx-label-plum-bg);
  --jx-badge-plum-fg: var(--jx-label-plum-fg);
  --jx-badge-cornflower-bg: var(--jx-label-cornflower-bg);
  --jx-badge-cornflower-fg: var(--jx-label-cornflower-fg);
  --jx-badge-clay-bg: var(--jx-label-clay-bg);
  --jx-badge-clay-fg: var(--jx-label-clay-fg);
  --jx-badge-fern-bg: var(--jx-label-fern-bg);
  --jx-badge-fern-fg: var(--jx-label-fern-fg);
  --jx-badge-success-bg: var(--jx-success-bg);
  --jx-badge-success-fg: var(--jx-success);
  --jx-badge-warn-bg: var(--jx-warn-bg);
  --jx-badge-warn-fg: var(--jx-warn);
  --jx-badge-danger-bg: var(--jx-danger-bg);
  --jx-badge-danger-fg: var(--jx-danger);
}

/* Per-app identity (set on each app's root) */
[data-app="notes"] {
  --jx-app: var(--jx-app-notes);
  --jx-app-chrome: var(--jx-app-notes-chrome);
}
[data-app="rss"] {
  --jx-app: var(--jx-app-rss);
  --jx-app-chrome: var(--jx-app-rss-chrome);
}
[data-app="review"] {
  --jx-app: var(--jx-app-review);
  --jx-app-chrome: var(--jx-app-review-chrome);
}
[data-app="issues"] {
  --jx-app: var(--jx-app-issues);
  --jx-app-chrome: var(--jx-app-issues-chrome);
}

/* Compact density (Issues) */
[data-density="compact"] {
  --jx-row-pad-y: 5px;
  --jx-row-pad-x: 10px;
  --jx-row-text: var(--jx-text-sm);
  --jx-row-leading: var(--jx-leading-sm);
}

@media (prefers-reduced-motion: reduce) {
  :root { --jx-duration-fast: 0ms; --jx-duration-base: 0ms; }
}
```
