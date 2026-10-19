# Launcher and Settings PWA

The app at `/` (launcher) and `/settings/` (Settings). Built into
`server/jesinia/static/launcher/`.

- **Launcher:** app tiles, plus a status strip for the KB, sync and agents, with a Sync
  now button.
- **Settings:** KB folder, git sync (Sync now, Resume, Initialize git), agents, tags,
  Review and Issues (below), RSS, conflicts, and token rotation.
- **Settings → Review** (`src/settings/Review.tsx`): the pre-review default and the agent,
  the standards folders, each repo's overrides (watched, pre-review, automated reviewer,
  metrics), and _What Jesinia thinks you know_, where you mark a folder or topic known or
  unknown, or clear the mark. Folder names there are shown as text, with hidden characters as
  visible tokens (`VisibleText` from `@jesinia/shell`).
- **Settings → Issues** (`src/settings/Issues.tsx`): the suggestions runner, the watched repos,
  and per repo its label schema (source, aliases, areas, and the repo's labels not mapped yet),
  with the label setup and migration previews and their apply. Every save sends only what it
  changes; label names are shown with hidden characters as visible tokens.

## Development

```bash
pnpm --filter @jesinia/launcher dev     # Vite; proxies /api to a server on 127.0.0.1
pnpm --filter @jesinia/launcher test
make build                              # build every app for the server to serve
```

The dev server proxies to the default port; change `vite.config.ts` if your server runs
elsewhere. Sign in with `jesinia login`. End-to-end tests are in
`web/tests/e2e/launcher.spec.ts` (`make test-e2e`).
