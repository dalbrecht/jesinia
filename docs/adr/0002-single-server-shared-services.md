# ADR 0002: One server, internal apps, shared services

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §3; [architecture conventions](../conventions/architecture.md)

## Context

Jesinia will grow several apps: Notes now, RSS, Review and Issues later. They all need
the same capabilities: the knowledge base, search, agents, GitHub and fetching. Separate
processes per app would multiply installs, ports, auth and databases on one Mac.

## Decision

- **One Python process** (FastAPI and uvicorn) hosts everything, bound to `127.0.0.1`.
- Apps are **internal modules** under `server/jesinia/apps/`. Cross-cutting capabilities
  are **shared services** under `server/jesinia/services/`. The only dependency
  direction is `core ← services ← apps`, and apps never import each other. Code that a
  second app needs is promoted into a service.
- Each service offers three faces over one application layer: a Python interface,
  HTTP routes under `/api/<service>/`, and MCP tools named `<service>_<verb>`.
- Inside each module, Clean Architecture layers (`domain ← application ←
  infrastructure ← api`). import-linter enforces both rule sets in CI.
- One SQLite database per module, each classed as *derived*, *cache* or *history*.

## Consequences

- One install, one port, one token, one log, and one `/healthz`.
- A misbehaving module can't take others down at startup: a module whose `start()`
  fails is reported unhealthy and its routes answer 503.
- The PWAs, other apps and terminal agents all exercise the same code path.
- Promoting shared code into a service costs a little upfront; sideways imports would
  cost more later.
