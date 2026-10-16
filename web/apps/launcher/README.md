# Launcher and Settings PWA

The app at `/` (launcher) and `/settings/` (Settings). Built into
`server/jesinia/static/launcher/`.

- **Launcher:** app tiles, plus a status strip for the KB, sync and agents, with a Sync
  now button.
- **Settings:** KB folder, git sync (Sync now, Resume, Initialize git), agents,
  conflicts, and token rotation.

## Development

```bash
pnpm --filter @jesinia/launcher dev     # Vite; proxies /api to a server on 127.0.0.1
pnpm --filter @jesinia/launcher test
make build                              # build every app for the server to serve
```

The dev server proxies to the default port; change `vite.config.ts` if your server runs
elsewhere. Sign in with a link from `jesinia status`. End-to-end tests are in
`web/tests/e2e/launcher.spec.ts` (`make test-e2e`).
