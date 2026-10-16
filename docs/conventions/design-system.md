# Design system

`@jesinia/design` (`web/packages/design`) implements the identity in ADR 0008. The
authoritative sources are [tokens](../design/tokens.md) and
[components](../design/components.md); this page covers usage.

## Tokens

- Three tiers:
  - **primitives** (`--jx-paper-*`, `--jx-sage-*`, …), never used by components;
  - **semantic roles** (`--jx-bg`, `--jx-fg`, `--jx-accent`, `--jx-success`, …);
  - **component tokens** (`--jx-button-*`, `--jx-row-*`, …).
- Light and dark themes follow `prefers-color-scheme`. `data-theme="light|dark"` on any
  ancestor overrides it.
- Each app sets `data-app="<app>"`, which defines `--jx-app` and `--jx-app-chrome`. Apps
  may override radii, density and their own `--jx-<app>-*` tokens, but never the
  semantic colors, focus, fonts or motion.
- `tokens.md` §5 lists every text and UI color pair with its minimum contrast (4.5:1 for
  text, 3:1 for UI). The contrast test checks each pair in both themes.

## Fonts

Bricolage Grotesque (UI), Newsreader (reading) and JetBrains Mono (code), all OFL-1.1
and self-hosted through `@fontsource-variable` (`@jesinia/design/fonts.css`).

## Components

Button, Input, Panel, List and Row, Badge, Tabs, Toast (`ToastProvider`, `useToast`),
ChatTranscript, CommandPalette, Banner (`BannerRegion`) and StatusIndicator.

- Every interactive component has visible focus (the shared ring), a 24 px minimum
  target, and a `loading` state that keeps focus.
- Status is never shown by color alone: badges need an icon, and banners carry words.
- No colored left borders or accent edges on panels.

## Shell

`@jesinia/shell`'s `AppShell` gives every PWA:

- a title-bar header (Window Controls Overlay) with the app glyph, its controls, the
  status dot, ⌕ and Settings;
- blocking banners: KB unavailable, sync paused, signed out;
- the ⌘K palette (app results, KB search, commands);
- toasts and theme.

## Demo

`make build` puts a demo of every token and component, in both themes, at
`http://127.0.0.1:5374/design/`.
