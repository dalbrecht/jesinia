# @jesinia/shell

The frame every Jesinia PWA uses.

## Usage

```tsx
import { AppShell } from "@jesinia/shell";

<AppShell app={{ id: "notes", name: "Notes", glyph: "/notes/icons/monochrome-512.png" }} ownSearch>
  {(status) => <Workspace status={status} />}
</AppShell>;
```

`AppShell` provides:

- the typed client (`useClient()`) and toasts;
- a title-bar header (Window Controls Overlay) with the app glyph, app controls, the
  status dot, ⌕ and Settings;
- blocking banners: KB unavailable, sync paused (with Resume), signed out;
- the ⌘K palette (app results, KB search, shell commands).

The children render-prop receives `ServerStatus` (health, sync, conflicts, runners),
kept current over `/api/events`.

## Also exported

- `APPS`: the suite, for launcher tiles and "Open …" commands.
- `registerServiceWorker(scope)` and `sw.js`, the minimal service worker that makes an
  app installable.
- `readTheme` / `applyTheme` / `cycleTheme`.
- Test helpers: `fakeClient`, `baseRoutes`, `syncStatus`.

## Development

`pnpm --filter @jesinia/shell test` and `typecheck`; `make ci` runs both.
