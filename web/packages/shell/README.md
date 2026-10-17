# @jesinia/shell

The frame every Jesinia PWA uses.

## Usage

```tsx
import { AppShell } from "@jesinia/shell";

<AppShell app={{ id: "notes", name: "Notes", glyph: "/notes/icons/monochrome.svg" }} ownSearch>
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
- `ChatRail` (with its reducer: `reduce`, `EMPTY`, `ChatState`, `lineDiff`): the agent chat
  shared by Notes and RSS Engage. It takes `session`, `onSessionNeeded(runner)` (start one for
  the first message) and `footer(chat)`, the app's own actions (Save note, Keep with this
  conversation). Agent replies render through `renderMarkdown`.
- `SafeMarkdown` / `renderMarkdown` / `safeHttpUrl` (ruling R17): untrusted Markdown through
  react-markdown with raw HTML skipped and http(s)-only URLs; never `innerHTML`. Images are off
  unless `images` is set (feed article views only). With them off, an image is its alt text
  and host as plain text. `renderMarkdown`, the chat's renderer, also shows each link's host
  and full href (R24), so agent output never loads anything.
- `TagPicker`: tag chips with catalog suggestions and near-duplicate checks;
  `suggestOnOpen` asks for suggestions once as it opens (RSS Keep). Its ref
  (`TagPickerHandle.commitDraft`) adds a still-typed tag before a save.
- Test helpers: `fakeClient`, `baseRoutes`, `syncStatus`.

## Development

`pnpm --filter @jesinia/shell test` and `typecheck`; `make ci` runs both.
