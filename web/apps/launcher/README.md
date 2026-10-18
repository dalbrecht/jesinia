# Launcher and Settings PWA

The app at `/` (launcher) and `/settings/` (Settings). Built into
`server/jesinia/static/launcher/`.

- **Launcher:** app tiles, plus a status strip for the KB, sync and agents, with a Sync
  now button.
- **Settings:** KB folder, git sync (Sync now, Resume, Initialize git), agents, tags,
  Review (below), RSS, conflicts, and token rotation.
- **Settings → Review** (`src/settings/Review.tsx`): the pre-review default and the agent,
  the standards folders, each repo's overrides (watched, pre-review, automated reviewer,
  metrics), and _What Jesinia thinks you know_, where you mark a folder or topic known or
  unknown, or clear the mark. Folder names there are shown as text, with hidden characters as
  visible tokens (`VisibleText` from `@jesinia/shell`).

## Development

```bash
pnpm --filter @jesinia/launcher dev     # Vite; proxies /api to a server on 127.0.0.1
pnpm --filter @jesinia/launcher test
make build                              # build every app for the server to serve
```

The dev server proxies to the default port; change `vite.config.ts` if your server runs
elsewhere. Sign in with a link from `jesinia status`. End-to-end tests are in
`web/tests/e2e/launcher.spec.ts` (`make test-e2e`).
