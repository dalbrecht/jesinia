# @jesinia/client

A typed client for the Jesinia HTTP API, plus a resilient server-sent-events helper.

## Usage

```ts
import { createClient, subscribe, UNAUTHORIZED_EVENT } from "@jesinia/client";

const client = createClient(); // same origin, cookie auth
const { data } = await client.GET("/api/kb/tree");

const stop = subscribe("/api/events?topics=kb.page_changed", (e) => console.log(e.data), {
  events: ["kb.page_changed"],
});
window.addEventListener(UNAUTHORIZED_EVENT, () => {
  /* the browser session ended: show a "sign in again" message */
});
```

- `createClient()` wraps `openapi-fetch` over types generated from the server's OpenAPI
  document. A 401 dispatches `UNAUTHORIZED_EVENT`.
- `subscribe()` reconnects on error and resumes with `after=<last id>`, so events are
  neither lost nor repeated.

## Regenerating types

After changing server routes, run `make gen-client` from the repo root. It runs
`jesinia openapi` against a throwaway home and writes `src/openapi.d.ts`, which is
committed.

## Development

`pnpm --filter @jesinia/client test` and `typecheck`; `make ci` runs both.
