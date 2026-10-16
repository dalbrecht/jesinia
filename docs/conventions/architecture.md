# Architecture

One server process hosts everything (ADR 0002).

## Layers

```
server/jesinia/
  core/        config, paths, auth, event bus, scheduler, db helpers, app factory, SSE, static PWAs
  services/    shared capabilities: kb, search, kbsync, agents, github, fetch
  apps/        products: notes (later rss, review, issues)
  mcp/         the /mcp endpoint aggregating every module's tools
  cli/         the `jesinia` command
```

- `core ← services ← apps`. Services never import apps, and **apps never import each
  other**. When a second app needs something an app built, promote it into a service
  with its own interface and tests.
- Inside every service and app: `domain ← application ← infrastructure ← api`. `domain`
  is pure (100% coverage target), `application` holds use cases and ports (`Protocol`s),
  `infrastructure` implements the ports (SQLite, filesystem, subprocesses), and `api`
  holds the HTTP routes and MCP tools.
- import-linter enforces both rule sets (`make imports`, part of `make ci`).

## A module

Every service and app is a `Module` (`core/module.py`) with a `name`, a `router()`, a
list of `mcp_tools()`, async `start(ctx)` and `stop()`, and a `health()`.

- Modules start in the order of `jesinia/registry.py` and stop in reverse. A module whose
  `start` raises is reported unhealthy in `/healthz`, and its routes answer 503; the
  others keep running.
- Modules find each other's services in `ctx.services` (`kb`, `search`, `agents`, …),
  never by importing another module's internals.
- HTTP routes are for the PWAs, the owner. They need the session cookie or the install
  token, and agent session tokens get 403. Agents reach Jesinia through MCP.

## One face, three surfaces

Each capability is implemented once in its application layer and exposed as:

1. a Python interface for in-process callers;
2. HTTP under `/api/<module>/…` for the PWAs (typed in the web client via OpenAPI:
   `make gen-client`);
3. MCP tools named `<module>_<verb>` (wire names match `^[a-zA-Z0-9_-]{1,64}$`). Tools
   that write set `writes=True`, and may supply a `preview` that tells the approval card
   what the write would really do.

## Data

One SQLite file per module in `~/Library/Application Support/jesinia/`, with numbered
SQL migrations (`migrations/0001_*.sql`, a `schema_version` table). Each database
declares a class:

| Class | Examples | If lost |
|---|---|---|
| derived | `kb`, `search`, `notes` | rebuilt from the KB (`jesinia reindex`) |
| cache | `github`, `fetch` | discarded; rows older than 30 days are pruned |
| history | `sessions` | agent transcripts; included in `jesinia backup` |

## Events

An in-process bus carries events such as `kb.page_changed`, `kb.root_changed` and
`kbsync.status`. `GET /api/events?topics=…` streams an allowlisted subset to the PWAs
as server-sent events, resumable from the last event id.

## Web

`web/` is a pnpm workspace:

- `packages/design` holds tokens, reset, fonts and components;
- `packages/client` is the typed API client and SSE helper;
- `packages/shell` is the app frame;
- `apps/*` are the PWAs, built into `server/jesinia/static/<app>/` and served at
  `/<app>/` (the launcher owns `/` and `/settings/`).

The Makefile is the only entry point for both toolchains.
