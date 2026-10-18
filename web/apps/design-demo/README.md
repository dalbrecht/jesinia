# Design demo

A reference page that renders every `@jesinia/design` token and component in both
themes. `make build` puts it at `server/jesinia/static/design/`, served at `/design/`.
`/design/drag.html` exercises the drag-and-drop primitives in a real browser; the e2e suite
(`web/tests/e2e/dnd.spec.ts`) drives it.

```bash
pnpm --filter @jesinia/design-demo dev
```
