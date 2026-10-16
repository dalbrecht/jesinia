# ADR 0001: License under AGPL-3.0-or-later

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §8 (licensing); [`LICENSE`](../../LICENSE)

## Context

Jesinia is a personal tool that runs a network server (a local HTTP server and an MCP
endpoint). Its value is in the code: the server, the apps and the agent bridge. If someone
improves it and offers it to others as a service, those improvements should flow back.

## Decision

Jesinia is licensed **AGPL-3.0-or-later**. Every source file carries
`SPDX-License-Identifier: AGPL-3.0-or-later` in a comment, and CI checks it
(`make spdx`). Third-party dependencies must be license-compatible, and fonts are
OFL-1.1, Apache-2.0 or MIT (ADR 0008).

## Consequences

- Anyone who runs a modified Jesinia for other users over a network must offer them the
  source.
- New files need the SPDX header, or CI fails.
- Dependencies with incompatible licenses (for example, proprietary SDKs) can't be
  bundled.
