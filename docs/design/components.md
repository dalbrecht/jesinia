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

**Link buttons (`href`).** The native `disabled` attribute does nothing on `<a>`, so a
disabled link button:

- renders `<a role="link" aria-disabled="true" tabindex="-1">` with the `href` removed,
  so it can't navigate and leaves the tab order, matching a disabled `<button>`;
- ignores clicks and Enter (the handler calls `preventDefault`).

`loading` is not allowed together with `href`; the type rejects it. A link that starts
work becomes a `<button>`.

**ARIA**

- Native `<button type="button">` by default; `type="submit"` must be explicit.
- An icon-only button requires `aria-label`. The type makes `label` or `aria-label`
  mandatory.
- A toggle button uses `aria-pressed`.
- `loading` adds `aria-busy="true"`. The label does not change, so screen readers don't
  re-announce it.

**Props**

```ts
type ButtonBase = {
  variant?: "primary" | "secondary" | "ghost" | "danger"; // default "secondary"
  size?: "sm" | "md";                                       // default "md"
  icon?: ReactNode;          // leading
  iconTrailing?: ReactNode;
} & (
  | { children: ReactNode }                                    // labelled
  | { children?: never; "aria-label": string; icon: ReactNode } // icon-only
);

type ButtonProps =
  | (ButtonBase & { href?: undefined; loading?: boolean }
      & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children">)  // renders <button>
  | (ButtonBase & { href: string; loading?: never; disabled?: boolean }
      & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "children" | "href">); // renders <a>;
      // disabled → aria-disabled, href removed, tabindex -1

// ref is HTMLButtonElement or HTMLAnchorElement to match the rendered element.
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
type InputCommon = {
  label: string;
  hideLabel?: boolean;
  help?: ReactNode;
  error?: ReactNode;         // presence sets invalid
  loading?: boolean;
  leading?: ReactNode;
  trailing?: ReactNode;      // e.g. <Button size="sm">Validate</Button>
};

type InputProps =
  | (InputCommon & {
      multiline?: false;
      variant?: "text" | "search" | "path";
    } & Omit<InputHTMLAttributes<HTMLInputElement>, "size">)       // renders <input>
  | (InputCommon & {
      multiline: true;                                            // composer
      onSubmit?: (value: string) => void;                         // Enter (not Shift+Enter, not during IME)
    } & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "onSubmit">); // renders <textarea>

// ref is HTMLInputElement or HTMLTextAreaElement to match the rendered element.
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
A tab panel sits below. A tab may carry a `title`: its full text on hover, when its label is shortened to fit.

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
  tabs: { id: string; label: string; count?: number; disabled?: boolean; title?: string }[];
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

// Mounted once by the app shell. Provides the useToast context and renders the
// toast region (landmark + live regions) after its children.
function ToastProvider(props: { children: ReactNode }): JSX.Element;

function useToast(): { show: (o: ToastOptions) => string; dismiss: (id: string) => void };

// The presentational toast that ToastProvider renders for each entry. Exported for the
// demo page and tests; apps call useToast().show instead of rendering it.
function Toast(props: ToastOptions & { id: string; onDismiss: (id: string) => void }): JSX.Element;
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

Grouping: consecutive tool-use messages between two agent messages collapse into one row
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
bottom. "At the bottom" means the remaining distance
`scrollHeight - scrollTop - clientHeight` is at most 48 px. It is measured on scroll
events, and once more before each content append.

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

type ChatMessage =
  | { id: string; kind: "user"; text: string; at: string }
  | { id: string; kind: "agent"; markdown: string; streaming: boolean; at: string }
  | { id: string; kind: "tool"; name: string; summary: string; detail?: string; running: boolean }
  | { id: string; kind: "error"; message: string; retryable: boolean }
  | { id: string; kind: "pending-write"; path: string; summary: string; diff: string;
      state: "awaiting" | "approving" | "approved" | "rejected" | "failed" | "superseded";
      error?: string };

type ChatTranscriptProps = {
  runner: Runner;
  messages: ChatMessage[];
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
| terminal attached | A neutral row "Continued in Claude Code · live", with activity appended as normal messages (spec §4.3) |

---

## 9. Shell components (added in Task 5)

The shell decisions in [the wireframes index](wireframes/README.md#shell-decisions)
add three shared components. They follow every rule in §0.

### 9.1 CommandPalette (⌘K)

**Anatomy:**

- A modal dialog, 640 px wide (full width minus `--jx-space-4` on narrow), placed 80 px
  from the top, over a scrim.
- Query field (48 px, `--jx-text-lg`).
- Result groups, in order:
  - **In this app**: app-provided results, omitted on the launcher and in Settings.
  - **Knowledge base**: the `search` service.
  - **Commands**: open an app, open a Settings section, Sync now, New session, toggle
    theme.
- Each item: a label, optional secondary text (a path, a shortcut) aligned right, and a
  leading glyph for commands that open another app.
- Footer with key hints.

**Tokens:**

| Part | Tokens |
|---|---|
| Container | `--jx-panel-raised-bg`, `--jx-panel-raised-shadow`, `--jx-radius-lg` |
| Active item | `--jx-row-bg-selected` |
| Group label | `--jx-text-xs`, `--jx-fg-muted` |
| Scrim | `--jx-paper-950` at 25% over the page. This is the one sanctioned translucent primitive use, defined as `--jx-scrim` in Task 22 |

**Behavior:**

- Opens on ⌘K from anywhere, including from inside text fields. There is no palette
  field in the title bar: apps with their own search field show nothing extra, and
  other apps show a ⌕ icon button (wireframes README, shell decision 3).
- Empty query shows recent pages and the top commands.
- Results update on input with a 100 ms debounce. KB results arrive asynchronously, and
  the other groups never wait for them.
- Max 6 items per group, with "Show all n" as the last item.
- Opening an app uses a URL, so the browser or PWA decides between window and tab.

**Keyboard:**

| Key | Action |
|---|---|
| ↑ / ↓ | Move the active item across groups |
| Enter | Run the active item |
| ⌘Enter | Open the active item in a new window |
| Escape | Close; focus returns to the invoking element |
| Tab | Stays inside the dialog (focus trap) |

**ARIA:**

- The palette is `role="dialog"` with `aria-modal="true"` and `aria-label="Command palette"`.
- The field is `role="combobox"`, with `aria-expanded` and `aria-controls` pointing to a
  `role="listbox"`.
- Each group is `role="group"` with `aria-label`; items are `role="option"`.
- `aria-activedescendant` tracks the active item, so focus stays in the field.
- The result count is announced politely ("8 results").

**Props**

```ts
type PaletteItem = { id: string; label: string; detail?: string; group: "app" | "kb" | "command";
  icon?: ReactNode; onRun: (opts: { newWindow: boolean }) => void };

function CommandPalette(props: {
  appSearch?: (q: string, signal: AbortSignal) => Promise<PaletteItem[]>; // the "In this app" group
  commands?: PaletteItem[];                                               // app commands merged with shell commands
}): JSX.Element; // mounted once by AppShell; owns the ⌘K binding
```

### 9.2 Banner (blocking system conditions)

**Anatomy:**

- A full-width strip directly under the app header, one line (it wraps on narrow).
- Contents: status icon, a bold cause ("Sync paused:"), the detail and fix, and up to two
  actions (a `secondary` `sm` primary action and a `ghost` `sm` secondary one) aligned
  right.

**Tones:**

| Tone | Used for | Tokens |
|---|---|---|
| `warn` | Sync paused | `--jx-warn-bg` fill, `--jx-warn` icon and cause, `--jx-fg` detail |
| `danger` | KB missing or unwritable; every runner unavailable in an app that needs one | `--jx-danger-bg`, `--jx-danger`, `--jx-fg` |

**Rules:**

- Only *blocking* conditions get a banner (shell decision 3).
- Banners stack, most severe first, with at most 2 visible plus a "+n more" ghost button
  that opens the status popover.
- A banner can't be dismissed while its condition holds. It leaves on its own when the
  condition clears, and a success toast confirms ("Sync resumed").
- No colored edge; tone comes from fill, icon, and words.

**ARIA:**

- The container is `role="region"` with `aria-label="System status"`.
- A banner that appears after page load is announced once via `role="alert"` (danger)
  or `role="status"` (warn). Banners present at load aren't announced.

**Props**

```ts
function Banner(props: { tone: "warn" | "danger"; cause: string; children: ReactNode;
  action?: { label: string; onAction: () => void };
  secondaryAction?: { label: string; onAction: () => void } }): JSX.Element;
```

### 9.3 StatusIndicator (header dot and popover)

**Anatomy:**

- A 28 px ghost icon button in the app header containing a 9 px dot:
  - `--jx-success`: all clear.
  - `--jx-warn`: minor issues.
  - `--jx-danger`: a blocking condition is also present.
- Clicking opens a popover (a `raised` Panel, 340 px) listing each condition. Each item
  has an icon, a bold title, a one-line fix, and a link to the relevant Settings section.

**Minor conditions** (no banner): duplicate page ids, missing linked pages, one runner
unavailable while another works, a feed or GitHub fetch failing.

**Keyboard:** Enter or Space opens the popover. Escape closes it and returns focus to
the dot. Tab moves through the links inside.

**ARIA:**

- The button has `aria-haspopup="dialog"` and `aria-expanded`.
- Its `aria-label` summarizes the state: "Status: all clear", or "Status: 2 issues".
- The dot's color is never the only signal: the label carries the count, and the
  popover lists every item.

**Props**

```ts
type StatusItem = { id: string; severity: "minor" | "blocking"; title: string; detail: string; href?: string };
function StatusIndicator(props: { items: StatusItem[] }): JSX.Element;
```

## 10. Not in the foundation set

**Issues board columns**, the **Review quiz and metrics** widgets, and the **RSS feed
tree** are app-specific components (brief §10), specified in their product specs.

## 11. Drag and drop (added in Issues Task 11)

Drag and drop is a primary way to work in Issues ([the Issues spec](../specs/2026-10-16-issues-design.md)
§6.1), so it's wrapped once here and shared by every app. Built on `@dnd-kit/core` and
`@dnd-kit/sortable` (MIT). Four parts: `DndProvider`, `Draggable`, `DropTarget` and
`SortableList`.

**Anatomy:**

- **Drag preview:** a raised card that follows the pointer. It shows the first item's label and, for
  a multi-selection, a count chip ("3 issues"). Over a refused target it gains a `--jx-danger`
  border and the refusal's reason. `--jx-surface-raised`, `--jx-border`, `--jx-shadow-2`; the chip
  is `--jx-accent` on `--jx-accent-fg`.
- **Dragged items** stay in place at 45% opacity while the preview travels.
- **Target highlight:** while a drag is on, a `DropTarget` that would take the items shows a 1 px
  dashed `--jx-border-strong` outline (`available`); one that would refuse them dims to 60%
  (`unavailable`). Under the drag it fills `--jx-accent-subtle` with a 2 px `--jx-accent` outline
  (`over`), or `--jx-danger-bg` with a dashed `--jx-danger` outline and the reason in
  `--jx-danger` text (`refused`).
- **Insertion line:** in a `SortableList`, a 2 px `--jx-accent` line where the rows will land.
  Rows don't shift aside, so the line reads the same for one row, a multi-selection, or rows
  dragged in from elsewhere. No line is drawn where a drop would change nothing.

Each part exposes its state as `data-drop-state` (`idle`, `available`, `unavailable`, `over`,
`refused`), `data-dragging` and `data-insert-before`, for app styling through tokens and for tests.

**Inputs**

| Input | Behavior |
|---|---|
| Mouse, trackpad | Press and move 4 px to lift |
| Touch | Long-press 250 ms to lift (`LONG_PRESS_MS`); moving more than 5 px first scrolls instead |
| Keyboard | Space or Enter picks up; arrows move between drop targets in document order; Space or Enter drops; Escape cancels |
| Auto-scroll | While the pointer rests within 40 px of a scrolling list's or board's edge, it scrolls, faster nearer the edge, up to 16 px a tick. It scrolls the containers under the pointer as well as around the dragged item, so a drag from one pane into a long list in another works. If the inner container can't go further, the outer one scrolls |

With the keyboard, ↓ and → step to the next target and ↑ and ← to the previous; the first press
goes to the nearest target in that direction. In a list, each row stands for the slot before it,
plus an end slot; slots where a drop would change nothing are skipped.

**Motion:** highlights change over `--jx-duration-fast`. On drop the preview settles into place
over 200 ms (it mirrors `--jx-duration-base` and `--jx-ease`, since the Web Animations API can't
read custom properties). Under `prefers-reduced-motion: reduce` the preview doesn't settle and
highlights change at once.

**Announcements** (one assertive live region, present from mount; plain sentences)

| Moment | Example |
|---|---|
| Pick up | "Picked up 3 issues. Use the arrow keys to move, Space or Enter to drop, Escape to cancel." |
| Over a target | "Over Epic A." |
| Over a list position | "Over Next up, position 2 of 5." |
| Over a refused target | "Can't drop on Epic A: An epic can't contain itself." |
| Drop on a target | "Dropped 3 issues on Epic A." |
| Reorder | "Moved Fix sync to position 3 of 4 in Next up." |
| Drop into a list | "Dropped Fix sync in Next up at position 1 of 3." |
| No change | "Fix sync stayed where it was. Nothing changed." |
| Refused drop | "Can't drop on Epic A: An epic can't contain itself. Nothing changed." |
| Cancel | "Drag cancelled. Nothing changed." |

One item is named by its label, several by count, using the provider's `noun`.

**ARIA:** each draggable is `role="button"` with `aria-roledescription="draggable"` and
`aria-describedby` pointing at the instructions ("To pick up, press Space or Enter. …"). Focus
returns to the draggable after a drop or cancel. Every drag also needs a menu equivalent in the
app ("Move to epic…", "Add to queue…"); the primitives don't provide one.

**Props**

```ts
type DragItem = { id: string; label?: string; data?: unknown };
// Ruling IR17: a typed refusal with a reason, or null to accept. Never a string-or-bool.
type Refusal = { code: string; reason: string };
type Accepts = (items: readonly DragItem[]) => Refusal | null;
type DropTargetInfo = { id: string; label: string; data?: unknown };

// Mounted once per app. Refused drops never reach onDrop. Reorders inside one SortableList go
// to its onMove; drops into a list from outside arrive here with position = the insertion slot
// (0 before the first row, the list's length at the end), else position is null.
function DndProvider(props: {
  onDrop: (items: readonly DragItem[], target: DropTargetInfo, position: number | null) => void;
  noun?: { one: string; other: string }; // default "item" / "items"
  renderPreview?: (items: readonly DragItem[], refusal: Refusal | null) => ReactNode;
  children: ReactNode;
}): JSX.Element;

// Its root is the handle. With a group that includes it, the whole group drags (multi-select).
function Draggable(props: HTMLAttributes<HTMLDivElement> & {
  id: string; label: string; data?: unknown; group?: readonly DragItem[]; disabled?: boolean;
}): JSX.Element;

// An epic, board column, theme, Done or Remove zone. accepts omitted: takes anything.
function DropTarget(props: HTMLAttributes<HTMLDivElement> & {
  id: string; label: string; data?: unknown; accepts?: Accepts;
}): JSX.Element;

// A queue or an epic's children. accepts applies to items from outside; reorders are always allowed.
function SortableList<T extends DragItem>(props: {
  id: string; label: string; items: readonly T[];
  onMove: (order: string[], moved: readonly T[]) => void;
  renderItem: (item: T, state: { index: number; isDragging: boolean }) => ReactNode;
  selection?: readonly string[]; accepts?: Accepts; data?: unknown; className?: string;
}): JSX.Element;
```

Ids must be unique within a provider: a `Draggable`, a `DropTarget` and a `SortableList` can't
share one. Rows inside a `SortableList` are namespaced by the list, so the same issue can appear
in a list and as a `Draggable` elsewhere.
