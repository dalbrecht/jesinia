# Jesinia components

Phase 0, Task 4 of [the foundation plan](../superpowers/plans/2026-10-16-jesinia-foundation.md).
This spec covers the eight core components of `@jesinia/design`. It builds on
[the brief](brief.md) and [the tokens](tokens.md). Every token named here is defined in
`tokens.md`; this spec adds no new color tokens. [`tokens.html`](tokens.html) renders
the components' default and selected states in both themes.

## 0. Rules for every component

**States.** Every interactive component has `default`, `hover`, `active` (pressed),
`focus-visible`, and `disabled`. Components that can wait on work also have `loading`.

- `hover` and `active` change fill only. Never layout, border width, or weight. Rows are
  the exception: selection makes the title weight heavier.
- `focus-visible` is always the shared ring:
  `outline: var(--jx-focus-width) solid var(--jx-focus); outline-offset: var(--jx-focus-offset)`.
  It never shows on mouse focus. It is never removed or recolored by an app.
- `disabled` uses the native `disabled` attribute, so the element leaves the tab order.
  `loading` uses `aria-disabled="true"` plus `aria-busy="true"`, so the element keeps
  focus (a button the user just pressed must not drop focus to `<body>`).

**Tokens.** Components read component tokens (tier 3) and semantic roles (tier 2), never
primitives. App overrides follow tokens.md §4 ("App-overridable tokens").

**Motion.** Fill and color changes use `--jx-duration-fast` with `--jx-ease`. Things
that appear or leave use `--jx-duration-base`. Under `prefers-reduced-motion: reduce`,
both durations are `0ms`, and nothing translates or scales.

**Props.** React function components in TypeScript. Each one spreads the remaining
native attributes onto its root element and forwards `ref`. Class names are an
implementation detail. Apps style through tokens, not by overriding classes.

**Copy.** Labels follow the brief's voice: sentence case, verbs on buttons, no
exclamation marks.

**Narrow width (390 px).** Hit targets are at least 24×24 px (WCAG 2.5.8) at every
width. At narrow width, `md` controls are used in place of `sm`, so targets reach 32 px.

---

## 1. Button

**Anatomy:** a root `<button>` (or `<a>` when `href` is set) containing an optional
leading icon, the label, and an optional trailing icon. In `loading`, a spinner replaces
the leading icon slot (or is inserted before the label when there is no icon), and the
label stays visible, so the width doesn't jump.

**Variants**

| Variant | Use | Tokens |
|---|---|---|
| `primary` | The one main action in a view or card ("Save note", "Approve change") | `--jx-button-primary-bg`, `-bg-hover`, `-bg-active`, `--jx-button-primary-fg` |
| `secondary` | Alternatives and cancels ("Reject", "Cancel", "Validate") | `--jx-button-secondary-bg`, `-bg-hover`, `-fg`, `-border` |
| `ghost` | Low-emphasis and toolbar actions ("More", icon buttons) | `--jx-button-ghost-bg-hover`, `--jx-button-ghost-fg` |
| `danger` | Destructive or consequential actions ("Rotate token") | `--jx-button-danger-bg`, `--jx-button-danger-fg` |

Sizes: `sm` uses `--jx-button-height-sm`, `-pad-x-sm`, `-text-sm`; `md` (the default)
uses the `-md` equivalents. Every size uses `--jx-button-radius` and
`--jx-button-weight`.

Use at most one `primary` per region. `danger` never sits next to `primary`; it pairs
with `secondary` ("Cancel").

**States**

| State | Treatment |
|---|---|
| hover | Variant's `-bg-hover`. `danger` darkens via `filter: brightness(0.95)` in light and `brightness(1.08)` in dark. No new token |
| active | `primary`: `-bg-active`. Others: the hover fill |
| focus-visible | Shared ring |
| disabled | `opacity: var(--jx-button-disabled-opacity)`, `cursor: not-allowed`, native `disabled` |
| loading | Spinner (16 px, `currentColor`, rotates at 800 ms per turn; static under reduced motion), `aria-busy`, `aria-disabled`; clicks are ignored |

**Keyboard:** Enter and Space activate. When rendered as `<a>`, only Enter.

**ARIA**

- Native `<button type="button">` by default; `type="submit"` must be explicit.
- An icon-only button requires `aria-label`. The type makes `label` or `aria-label`
  mandatory.
- A toggle button uses `aria-pressed`.
- `loading` adds `aria-busy="true"`. The label does not change, so screen readers don't
  re-announce it.

**Props**

```ts
type ButtonProps = {
  variant?: "primary" | "secondary" | "ghost" | "danger"; // default "secondary"
  size?: "sm" | "md";                                       // default "md"
  loading?: boolean;
  icon?: ReactNode;          // leading
  iconTrailing?: ReactNode;
  href?: string;             // renders <a>
} & (
  | { children: ReactNode }                              // labelled
  | { children?: never; "aria-label": string; icon: ReactNode } // icon-only
) & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children">;

function Button(props: ButtonProps): JSX.Element;
```

---

## 2. Input

**Anatomy:**

- Visible label (`<label>`).
- Field: `<input>`, or `<textarea>` when `multiline`, with optional leading and trailing
  adornments (a search icon, or an inline action such as "Validate").
- Help text.
- Error text. It replaces the help text while `invalid`. They don't stack.

**Variants:** `text` (default), `search` (leading icon, clear button when filled,
`type="search"`), `path` (monospace field via `--jx-font-mono`, for the KB path),
`multiline` (auto-grows from 1 to 8 rows, used for the chat composer).

**Tokens:** `--jx-input-height`, `-radius`, `-pad-x`, `-bg`, `-fg`, `-placeholder`,
`-border`, `-border-focus`, `-border-invalid`, `-help-fg`, `-error-fg`.

**States**

| State | Treatment |
|---|---|
| hover | Border stays `--jx-input-border` (it already meets 3:1); the background moves to `--jx-surface-hover` only for `search` |
| focus-visible | Border `--jx-input-border-focus` plus the shared ring. Inputs show the ring on mouse focus too, since text fields always match `:focus-visible` |
| disabled | `opacity: 0.45`, native `disabled`. Label and help stay full contrast |
| invalid | Border `--jx-input-border-invalid`; error text in `--jx-input-error-fg` with a leading "×" icon. Color is never the only cue |
| loading | Used when validating (KB path). A trailing spinner adornment and `aria-busy` on the field. The field stays editable |

**Keyboard**

- Standard text editing.
- `search`: Escape clears a filled field. When the field is empty, Escape is left to the
  container (to close a palette, for example).
- `multiline` composer: Enter sends and Shift+Enter inserts a newline. IME composition
  never sends.

**ARIA**

- `<label for>` is always rendered. A visually hidden label is allowed only where a
  visible one would duplicate an adjacent heading.
- `aria-describedby` points to the help or error text.
- `aria-invalid="true"` when invalid.
- Error text has `id` and `role="alert"` only when it appears after a submit. Live
  validation updates silently and is read on focus.
- `required` uses the native attribute plus "(required)" in the label. Never an asterisk
  alone.

**Props**

```ts
type InputProps = {
  label: string;
  hideLabel?: boolean;
  help?: ReactNode;
  error?: ReactNode;         // presence sets invalid
  variant?: "text" | "search" | "path";
  multiline?: boolean;       // composer
  loading?: boolean;
  leading?: ReactNode;
  trailing?: ReactNode;      // e.g. <Button size="sm">Validate</Button>
  onSubmit?: (value: string) => void; // multiline: Enter
} & Omit<InputHTMLAttributes<HTMLInputElement>, "size">;

function Input(props: InputProps): JSX.Element;
```

---

## 3. Panel

**Anatomy:** a root `<section>` containing an optional header (title plus actions, laid
out on one row that wraps), the body, and an optional footer.

**Variants**

| Variant | Use | Tokens |
|---|---|---|
| `default` | Grouped content on the page: settings groups, lists | `--jx-panel-bg`, `-border`, `-radius`, `-pad`, `-shadow` (none) |
| `raised` | Floating: popovers, menus, the chat pane when popped out | `--jx-panel-raised-bg`, `--jx-panel-raised-shadow` |
| `flush` | A list that fills the panel edge to edge (no body padding) | `default` tokens, `padding: 0` |

The title uses `--jx-panel-title-text` and `--jx-panel-title-weight`. Panels never get an
accent edge or a colored left border (brief §5).

**States:** none of its own. A panel isn't interactive. Collapsible groups are a
disclosure button in the header (a `ghost` Button with `aria-expanded`), not a panel
state.

**Keyboard:** none. Focus moves through its contents.

**ARIA:** when `title` is set, the `<section>` gets `aria-labelledby` pointing to the
title's heading element, which renders at the level given by `headingLevel`
(default 2). Without a title it renders a `<div>`.

**Props**

```ts
type PanelProps = {
  variant?: "default" | "raised" | "flush";
  title?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
} & HTMLAttributes<HTMLElement>;

function Panel(props: PanelProps): JSX.Element;
```

---

## 4. List / Row

**Anatomy**

- `List`: the container, with an optional sticky header slot (a filter bar).
- `Row`:
  - Leading slot: an unread dot, a runner glyph, or an app glyph.
  - Title.
  - Excerpt: one line, truncated with an ellipsis, Desk density only.
  - Metadata: source, age, labels.
  - Trailing slot: badges, counts, or a ghost action that appears on hover or focus.

**Variants**

| Variant | Use |
|---|---|
| density `desk` (default) | RSS, Review, Notes, Settings lists |
| density `compact` | Issues. Set by `[data-density="compact"]` on any ancestor, or by the `density` prop |
| selection `none` | Plain lists, such as search results that navigate on Enter |
| selection `single` | Master–detail lists (feed items, PRs) |
| selection `multiple` | Bulk triage (Issues) |

**Tokens:** `--jx-row-pad-y`, `-pad-x`, `-text`, `-leading` (density); `--jx-row-bg`,
`-bg-hover`, `-bg-selected`, `-title-weight-selected`, `-divider`, `-meta-fg`,
`-unread-dot`.

**States**

| State | Treatment |
|---|---|
| hover | `--jx-row-bg-hover` |
| active | `--jx-row-bg-hover` |
| selected | `--jx-row-bg-selected` plus `--jx-row-title-weight-selected`. No border, rail, or check icon. In `multiple`, a checkbox in the leading slot shows selection |
| focus-visible | Shared ring drawn inset (`outline-offset: -2px`) so adjacent rows don't clip it |
| disabled | `opacity: 0.45`, `aria-disabled`; skipped by arrow keys |
| unread | Leading dot in `--jx-row-unread-dot` plus a `medium` title weight; the read state is regular weight with no dot |
| loading | A skeleton row: three bars in `--jx-surface-sunken` at the row's real height. No shimmer under reduced motion |

**Keyboard**

The list is a single tab stop with a roving `tabindex`.

| Key | Action |
|---|---|
| ↓ / ↑ | Move focus. In `single`, selection follows focus |
| Home / End | First or last row |
| Enter | Activate (open the item, navigate) |
| Space | Toggle the row in `multiple`. In `single`, same as Enter |
| Shift+↓ / Shift+↑ | Extend the selection in `multiple` |
| ⌘A | Select all in `multiple` |
| j / k | Aliases for ↓ / ↑ (Reeder-style), only when no text field is focused |

**ARIA**

- `selection: "none"` renders `role="list"` and `role="listitem"`. Each row contains its
  own link or button.
- `single` and `multiple` render `role="listbox"` (with `aria-multiselectable` for
  multiple) and `role="option"` with `aria-selected`.
- The list requires `aria-label` or `aria-labelledby`.
- Row content is plain text inside an option. Interactive children (trailing actions)
  are not allowed inside `option`. They move into a context menu, or the row uses
  `selection: "none"`.

**Props**

```ts
type ListProps<T> = {
  items: T[];
  getKey: (item: T) => string;
  renderRow: (item: T, state: { selected: boolean; focused: boolean }) => ReactNode;
  selection?: "none" | "single" | "multiple";
  selected?: string[];                        // controlled, keys
  onSelectionChange?: (keys: string[]) => void;
  onActivate?: (key: string) => void;         // Enter / double-click
  density?: "desk" | "compact";
  loading?: boolean;                          // renders skeleton rows
  empty?: ReactNode;                          // empty state copy
  "aria-label"?: string;
  "aria-labelledby"?: string;
};

type RowProps = {
  title: ReactNode;
  excerpt?: ReactNode;
  meta?: ReactNode;
  leading?: ReactNode;
  trailing?: ReactNode;
  unread?: boolean;
  disabled?: boolean;
};

function List<T>(props: ListProps<T>): JSX.Element;
function Row(props: RowProps): JSX.Element;
```

---

## 5. Badge

**Anatomy:** a pill containing an optional leading icon and a short label (1–2 words).

**Variants (tones)**

| Tone | Tokens | Rule |
|---|---|---|
| `neutral` | `--jx-badge-neutral-bg`, `-fg` | Counts, states like "draft" |
| `marigold` · `plum` · `cornflower` · `clay` · `fern` | `--jx-badge-<label>-bg`, `-fg` | KB tags and Issues labels. Apps map tag names to a tone deterministically (a stable hash of the name) |
| `success` · `warn` · `danger` | `--jx-badge-<status>-bg`, `-fg` | **An icon is required** (✓, !, ×). The type enforces it |

Size: one size, `--jx-badge-text`, `--jx-badge-pad-x`, `--jx-badge-radius`.

**States:** none. Badges are not interactive. A clickable tag filter is a `ghost` Button
that contains a Badge.

**Keyboard:** none.

**ARIA:**

- A plain `<span>`.
- A count badge next to a label must make sense read inline ("Unread 12"), or carry a
  visually hidden suffix ("12 unread").
- Status icons are `aria-hidden`, because the label carries the meaning.

**Props**

```ts
type BadgeProps =
  | { tone?: "neutral" | "marigold" | "plum" | "cornflower" | "clay" | "fern"; icon?: ReactNode; children: ReactNode }
  | { tone: "success" | "warn" | "danger"; icon: ReactNode; children: ReactNode };

function Badge(props: BadgeProps & HTMLAttributes<HTMLSpanElement>): JSX.Element;
```

---

## 6. Tabs

**Anatomy:** a tab list (a row of tabs sitting on a 1 px `--jx-border` baseline), with a
2 px indicator under the selected tab. Each tab has a label and an optional count Badge.
A tab panel sits below.

**Variants:** one visual style. When the tabs overflow, the tab list scrolls
horizontally with edge fades; tabs never wrap onto two lines.

**Tokens:** `--jx-tab-fg`, `-fg-hover`, `-fg-selected`, `-indicator`,
`-indicator-height`, `-pad-x`, `-height`, `-weight-selected`.

**States**

| State | Treatment |
|---|---|
| hover | `--jx-tab-fg-hover` |
| active | `--jx-tab-fg-hover` |
| selected | `--jx-tab-fg-selected`, `--jx-tab-weight-selected`, and the indicator in `--jx-tab-indicator` |
| focus-visible | Shared ring around the tab |
| disabled | `opacity: 0.45`, `aria-disabled`; arrow keys skip it |

When selection changes, the indicator slides between tabs over `--jx-duration-base`.
Under reduced motion it jumps.

**Keyboard** (WAI-ARIA Tabs pattern, **automatic activation**: panels are cheap
client-side views)

| Key | Action |
|---|---|
| Tab | Into the tab list (onto the selected tab), then out to the panel |
| ← / → | Previous or next tab, wrapping; selects it |
| Home / End | First or last tab |

**ARIA:**

- `role="tablist"` with `aria-label`.
- Each tab is `role="tab"` with `aria-selected` and `aria-controls`.
- The panel is `role="tabpanel"` with `aria-labelledby` and `tabindex="0"` when it has
  no focusable content.

**Props**

```ts
type TabsProps = {
  tabs: { id: string; label: string; count?: number; disabled?: boolean }[];
  selected: string;
  onSelect: (id: string) => void;
  "aria-label": string;
  children: (selectedId: string) => ReactNode; // renders the panel body
};

function Tabs(props: TabsProps): JSX.Element;
```

---

## 7. Toast

**Anatomy:**

- A toast region, fixed bottom-right on desktop and bottom-center on narrow, inset by
  `--jx-space-4`.
- A stack of up to 3 toasts, newest at the bottom.
- Each toast: a status icon, a message (one sentence), an optional action (a `ghost`
  Button, `sm`), and a close button (a `ghost` icon button, "Dismiss").

**Variants (tones):** `info` (no icon color, `--jx-fg-muted` icon), `success`
(`--jx-toast-icon-success`), `warn` (`--jx-toast-icon-warn`), `danger`
(`--jx-toast-icon-danger`). Every tone shares `--jx-toast-bg`, `-fg`, `-border`,
`-shadow`, `-radius`, and `-width`. Tone is shown by the icon and wording, never by a
colored fill or edge.

**Behavior**

| Rule | Value |
|---|---|
| Auto-dismiss | `info` and `success` after 5 s. `warn` and `danger` persist until dismissed. Any toast with an action persists |
| Pause | The timer pauses while the region is hovered or contains focus |
| Overflow | A 4th toast removes the oldest dismissible one. If none is dismissible, it queues |
| Enter / exit | Opacity 0→1 plus a 4 px rise over `--jx-duration-base`. Under reduced motion, opacity only |
| Not for | Anything needing a decision (use a pending-write card or an inline banner) or errors that block a flow (use an inline error). Toasts confirm and inform |

**States:** the toast itself isn't interactive. Its action and close buttons follow
Button states.

**Keyboard:**

- Toasts never take focus.
- F6 (and ⌥⌘T on macOS) moves focus into the toast region and back to where it was.
- Escape, with focus in the region, dismisses the focused toast.

**ARIA:**

- The region is a landmark: `role="region"` with `aria-label="Notifications"`.
- `info`, `success`, and `warn` toasts are announced through a polite live region
  (`role="status"`). `danger` toasts use `role="alert"`.
- The live region exists in the DOM from page load (an empty container), so the first
  announcement isn't lost.

**Props**

```ts
type ToastOptions = {
  tone?: "info" | "success" | "warn" | "danger";
  message: string;
  action?: { label: string; onAction: () => void };
  duration?: number | "persistent"; // overrides the tone default
};

function ToastRegion(): JSX.Element;        // mounted once by the app shell
function useToast(): { show: (o: ToastOptions) => string; dismiss: (id: string) => void };
```

---

## 8. ChatTranscript

A streaming-aware transcript of one agent session. The composer and runner picker are
composed around it by the chat pane (Task 5). They use Input (`multiline`) and Button and
are not part of this component.

### 8.1 Anatomy

- **Scroll container.** Messages are stacked with `--jx-chat-gap`.
- **Message.** Kinds are listed in 8.2.
  - Header (agent and pending-write only): a runner glyph, the runner name, and a
    relative time.
  - Body.
  - Footer (optional): actions.
- **Jump to latest:** a floating `secondary` `sm` Button with a count ("3 new"), shown
  only when the user has scrolled up and new content has arrived.
- **Announcer:** a visually hidden live region (see 8.4).

Runner glyph: a `--jx-runner-glyph-size` square with `--jx-runner-glyph-radius`. It
shows the monogram ("C" for Claude Code, "X" for Codex) in `--jx-runner-<r>-fg` on
`--jx-runner-<r>`. No vendor logos (brief §8).

### 8.2 Message kinds

| Kind | Visual | Tokens | Behavior |
|---|---|---|---|
| **user** | Right-aligned bubble, max 92% width | `--jx-chat-bubble-user-bg`, `-user-border`, `-radius`, `-pad` | Static. Text is pre-wrapped; Markdown is not rendered |
| **agent (streaming)** | Left, full width, `--jx-ai` fill, header | `--jx-chat-bubble-agent-bg`, `-agent-fg`, `--jx-chat-cursor` | Markdown renders incrementally (block-level re-render on newline). A quiet 2 px × 1em cursor in `--jx-chat-cursor` sits after the last character. It fades in and out at 1 s per cycle, and is static under reduced motion. `aria-busy="true"` |
| **agent (done)** | Same, no cursor | Same | `aria-busy` is removed. Footer offers "Copy" (ghost, sm) and, where the app allows, "Save to KB" |
| **tool use** | A collapsed single row between messages: "▸ kb.search "maintenance budget" · 3 results" in mono | `--jx-chat-tool-fg`, `--jx-chat-tool-font` | A disclosure button (`aria-expanded`). Expanding shows arguments and the result summary in a `--jx-surface-sunken` block, capped at 240 px with internal scroll. While running, a spinner replaces ▸ and the text reads "kb.search…" |
| **error** | Left, full width bubble with a "×" icon, message, and a **Retry** button | `--jx-chat-error-bg`, `--jx-chat-error-fg` | Copy gives the cause and the fix ("Claude stopped: the CLI is logged out. Run `claude login`, then Retry."). Retry resumes the session (spec §13). The transcript up to the error is kept |
| **pending write** | A raised card. Header: glyph and "Claude wants to edit **Notes/maintenance.md**". Body: a one-line summary ("Adds a 'Funding models' section, 12 lines") plus a "Show changes" disclosure with a unified diff in mono. Footer: **Approve change** (primary) and **Reject** (secondary) | `--jx-chat-pending-bg`, `-pending-border`, `-pending-shadow`; diff uses `--jx-surface-sunken` | See 8.3 |

Grouping: consecutive tool-use rows between two agent messages collapse into one row
("▸ 3 tool calls") that expands into the individual rows.

### 8.3 Pending-write card states

| State | Treatment |
|---|---|
| awaiting | As described in 8.2. The card arrives with the base-duration fade and rise. This is the brief's one allowed "warm moment" in chat |
| approving | Approve shows `loading`; Reject is disabled |
| approved | The card collapses (base duration) to a single row: "✓ Approved · Notes/maintenance.md" with a link to the page. `--jx-success` icon |
| rejected | Collapses to "Rejected · Notes/maintenance.md" in `--jx-fg-muted` |
| failed | Stays expanded with an inline error under the summary ("Couldn't write: the file changed on disk. Show changes again to review.") and the buttons re-enabled |
| superseded | When a newer write targets the same file before a decision, the older card collapses to "Replaced by a newer change" |

The agent's turn is blocked until a decision is made. The composer stays usable, so the
user can reply instead, and replying counts as a rejection: the card shows "Rejected ·
you replied".

### 8.4 Scrolling and announcements

**Auto-scroll rule:** the transcript sticks to the bottom while the user is at the
bottom. "At the bottom" means within 48 px of `scrollHeight`.

- When the user scrolls up, auto-scroll stops. New content then increments the "Jump to
  latest" counter instead of moving the view.
- Activating "Jump to latest", or scrolling back into the 48 px zone, re-engages
  auto-scroll.
- Sending a message always scrolls to the bottom and re-engages auto-scroll.
- Expanding a tool row or diff never auto-scrolls. The expanded content grows downward
  from where the user clicked.
- Scrolling uses `scroll-behavior: smooth` only for "Jump to latest", and only without
  reduced motion.

**Live-region policy:**

- The scroll container is `role="log"` with `aria-live="off"` and
  `aria-label="Conversation with <runner>"`. Streamed tokens are **never** announced one
  by one.
- A separate visually hidden announcer (`aria-live="polite"`, `aria-atomic="true"`)
  speaks only:
  - "Claude is responding" when a stream starts.
  - The full agent message when it completes, truncated to 300 characters plus
    "Message continues in the transcript".
  - "Claude wants to edit Notes/maintenance.md. Approve or reject." for a pending write.
- Errors are announced assertively: the error message's text goes into a
  `role="alert"` node.
- Tool-use rows are not announced.
- Focus is never moved by incoming content. The pending-write card's buttons are
  reachable by Tab from the transcript.

**Keyboard:**

- The scroll container is focusable (`tabindex="0"`), so keyboard users can scroll it.
- With focus in the transcript, `⌘↓` jumps to latest and `⌘↑` jumps to the first
  message.
- All actions are Buttons in DOM order.

### 8.5 Props

```ts
type Runner = { id: "claude" | "codex"; name: string };

type ChatItem =
  | { id: string; kind: "user"; text: string; at: string }
  | { id: string; kind: "agent"; markdown: string; streaming: boolean; at: string }
  | { id: string; kind: "tool"; name: string; summary: string; detail?: string; running: boolean }
  | { id: string; kind: "error"; message: string; retryable: boolean }
  | { id: string; kind: "pending-write"; path: string; summary: string; diff: string;
      state: "awaiting" | "approving" | "approved" | "rejected" | "failed" | "superseded";
      error?: string };

type ChatTranscriptProps = {
  runner: Runner;
  items: ChatItem[];
  onRetry?: (id: string) => void;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  onSaveToKb?: (id: string) => void;  // shown only when provided
  renderMarkdown?: (md: string) => ReactNode; // app supplies the KB-aware renderer
  empty?: ReactNode;                  // e.g. "Ask Claude about this page."
};

function ChatTranscript(props: ChatTranscriptProps): JSX.Element;
```

### 8.6 Empty, loading, and unavailable states

| State | Treatment |
|---|---|
| empty | Centered `--jx-fg-muted` line from `empty`, defaulting to "Ask \<runner\> about this \<subject\>." No illustration |
| loading (history) | Two skeleton bubbles (user right, agent left) in `--jx-surface-sunken` |
| runner unavailable | Rendered by the chat pane, not the transcript: an inline banner "Codex isn't available: not logged in. Run `codex login`, or switch to Claude." plus a runner switch (spec §13) |
| terminal attached | A neutral row "Continued in Claude Code · live", with activity appended as normal items (spec §4.3) |

---

## 9. Open questions for later tasks

- **Command palette (⌘K)** and **menus/popovers** are not in the foundation's eight. If
  Task 5 picks global search as a palette, it will be specified as a ninth component then.
- **Issues board columns** and **Review quiz/metrics** are app-specific components
  (brief §10), specified in their product specs.
