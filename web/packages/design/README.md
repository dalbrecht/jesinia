# @jesinia/design

Jesinia's design system: tokens, reset, fonts and React components. It implements
ADR 0008. The authoritative names and values are in
[`docs/design/tokens.md`](../../../docs/design/tokens.md) and
[`components.md`](../../../docs/design/components.md); usage rules are in
[design-system conventions](../../../docs/conventions/design-system.md).

## Usage

```ts
import "@jesinia/design/fonts.css"; // OFL fonts via @fontsource-variable
import "@jesinia/design/reset.css";
import "@jesinia/design/tokens.css";
import { Button, ChatTranscript, ToastProvider } from "@jesinia/design";
```

The package is consumed as TypeScript source by the workspace's Vite apps; there is no
build step.

- **Themes:** light and dark follow `prefers-color-scheme`. `data-theme="light|dark"`
  overrides it.
- **App identity:** set `data-app="<app>"` on an app's root.
- **Drag and drop:** mount one `DndProvider` per app and build with `Draggable`, `DropTarget`
  and `SortableList` (components.md §11). They wrap `@dnd-kit/core` and `@dnd-kit/sortable`.

## Development

```bash
pnpm --filter @jesinia/design test        # vitest + coverage (contrast test included)
pnpm --filter @jesinia/design typecheck
pnpm --filter @jesinia/design-demo dev    # every token and component, both themes
```

From the repo root, `make ci` runs all of this.
