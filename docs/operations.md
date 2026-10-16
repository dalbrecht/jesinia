# Operations

How Jesinia is installed, run, and wired to coding agents on one Mac. Task 27 extends this
page; this section records the agent registration commands, checked against Claude Code
2.1.301 and Codex 0.163.0 (`claude mcp --help`, `claude plugin --help`, `codex mcp --help`).

## Install and uninstall

- `./setup [--kb-path PATH] [--yes] [--dry-run]` checks the toolchain, runs
  `make install build`, then `jesinia install`.
- `jesinia install` writes `config.toml` (mode 600; the token is generated once and kept
  across re-installs), creates the data and log folders, writes
  `~/Library/LaunchAgents/com.jesinia.server.plist`, then runs
  `launchctl bootout gui/$UID/com.jesinia.server` (absence is fine) and
  `launchctl bootstrap gui/$UID <plist>`. It waits up to 20 s for `/healthz` and prints a
  one-time login link.
- The LaunchAgent runs `uv run --project <repo>/server jesinia serve` at login. It is
  restarted only after a crash (`KeepAlive.SuccessfulExit = false`). It gets the `PATH`
  and `SSH_AUTH_SOCK` that were set at install time, so git can push with your keys.
- `./setup --uninstall [--purge-data]` boots the agent out and removes the plist and the
  agent registrations. Data folders are removed only with `--purge-data`, after
  confirmation. The KB folder is never touched.
- `--dry-run` prints every action and changes nothing (tokens print as `***`).

## Agent registration

`install` asks before registering with each agent CLI it finds on `PATH` (`--yes` skips
the question). Registrations carry the install token, so `jesinia token rotate`
re-registers every agent that has one.

| Agent | Register | Remove |
|---|---|---|
| Claude Code | `claude mcp add --scope user --transport http jesinia http://127.0.0.1:4870/mcp --header "Authorization: Bearer <token>"` | `claude mcp remove --scope user jesinia` |
| Codex | `codex mcp add jesinia --url http://127.0.0.1:4870/mcp`, then a `[mcp_servers.jesinia.http_headers]` table with `Authorization = "Bearer <token>"` appended to `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`) | `codex mcp remove jesinia` (this also drops the headers table) |

`codex mcp add` has no header flag. `--bearer-token-env-var` would need `JESINIA_TOKEN`
in every shell that starts Codex. A static header in Codex's own config is the
documented alternative, and `codex mcp remove` rewrites the file without losing other
settings or comments.

The token sits in each agent's user config (`~/.claude.json` and `~/.codex/config.toml`),
just as it does in Jesinia's `config.toml`. It grants loopback-only access to this Mac's
server.

Sessions that Jesinia launches itself do not use these registrations. Each session gets
its own token through `--mcp-config` (Claude Code) or `-c mcp_servers.jesinia.*` (Codex),
with a strict tool allowlist.
