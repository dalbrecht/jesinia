# ADR 0009: Install, keep-alive, auto-update and the default port

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §6, §7; plan Tasks 20, 21, 26;
  [operations guide](../operations.md)

## Context

The spec planned a LaunchAgent that restarts only after a crash, port 4870, and an MCP
server bundled inside the Claude Code plugin. Implementing and using it surfaced better
defaults:

- The user wants Jesinia to stay up, run from a dedicated checkout, and update itself
  from `main`.
- Port 4870 isn't distinctive.
- A plugin-bundled MCP server can't carry the per-install token without an environment
  variable in every shell.

## Decision

- **Default port 5374** ("JESI" on a phone keypad). It is memorable, below the macOS
  ephemeral range, and clear of common development ports and AirPlay (5000/7000).
  `server.port` still overrides it.
- **Keep-alive:** the server LaunchAgent uses `KeepAlive: true` with a 10 s throttle, so
  launchd restarts it however it exits. `./setup --uninstall` is the way to stop it.
- **Auto-update (opt-in, `--auto-update`):**
  - A second LaunchAgent, `com.jesinia.updater`, runs `jesinia update` at login and every
    15 minutes.
  - It only fast-forwards a clean checkout on `main` to `origin/main`, then runs
    `make install build` and restarts the server.
  - If the build or the restarted server fails, it resets to the previous commit,
    rebuilds and restarts.
  - Runs are serialized with a lock file and logged to `update.log`.
- **Agent registration:**
  - The installer registers the MCP endpoint itself: `claude mcp add --scope user` with
    a header, and `codex mcp add` plus a static header in Codex's config.
  - The Claude Code plugin ships skills and the `/jesinia-note` command only.
  - Codex gets a managed block in `~/.codex/AGENTS.md`.
- **The plist is generated** with `plistlib` (no template file).

## Consequences

- Running Jesinia from a dedicated checkout (for example `~/apps/jesinia`) and enabling
  auto-update keeps it current without manual pulls. A development checkout should not
  enable auto-update.
- A bad commit on `main` costs at most one failed update attempt, which is then rolled
  back.
- Rotating the token re-registers both agents. The token sits in each agent's own user
  config, which is acceptable because it only grants loopback access.
