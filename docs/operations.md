# Operations guide

Install, run, update and remove Jesinia on a Mac, and fix what goes wrong. For the short
version, see the [README](../README.md#quickstart).

- [Requirements](#requirements)
- [Install](#install)
- [Signing in](#signing-in)
- [Everyday commands](#everyday-commands)
- [Where things live](#where-things-live)
- [Updating](#updating)
- [Agent registration](#agent-registration)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)
- [Developing alongside an install](#developing-alongside-an-install)

## Requirements

| Tool | Why | Install |
|---|---|---|
| macOS | launchd runs the server | — |
| git | the KB and Jesinia itself are git repositories | `xcode-select --install` |
| [uv](https://docs.astral.sh/uv/) | Python 3.12+ and the server's dependencies | `brew install uv` |
| Node 22+ with Corepack | builds the web apps (pnpm is pinned by the repo) | `brew install node`, then `corepack enable pnpm` |
| `gh` (optional) | GitHub features (later apps) | `brew install gh && gh auth login` |
| `claude` and/or `codex` (optional) | chat with an agent in Notes | install Claude Code or Codex and log in |

`./setup` checks these and prints the exact command for anything missing.

## Install

### Recommended: a dedicated checkout that updates itself

Keep the install separate from any development checkout, and let it follow `main`:

```bash
mkdir -p ~/apps
git clone --recurse-submodules https://github.com/dalbrecht/jesinia.git ~/apps/jesinia
cd ~/apps/jesinia
./setup --kb-path ~/kb --auto-update
```

### Options

| Option | Effect |
|---|---|
| `--kb-path PATH` | Your Markdown knowledge base folder. Without it, setup asks (default `~/kb`). The folder is created if missing; existing files are adopted, never moved |
| `--auto-update` | Install the update agent (see [Updating](#updating)) |
| `--yes` | Don't ask before registering agents or adding the `jesinia` command |
| `--dry-run` | Print every action and change nothing (tokens print as `***`) |

### What setup does

1. Checks the toolchain, then runs `make install build`. That installs the Python and JS
   dependencies and builds the PWAs into `server/jesinia/static/`.
2. Writes `config.toml` (mode 600) with a new install token. The token is kept on later
   runs.
3. Writes `~/Library/LaunchAgents/com.jesinia.server.plist` and starts it:
   - it runs `uv run --project <checkout>/server jesinia serve` at login;
   - `KeepAlive` is on, so launchd restarts it whenever it stops;
   - it gets the `PATH` and `SSH_AUTH_SOCK` from the setup shell, so git can push with
     your keys and agents are found.
4. Waits for `http://127.0.0.1:5374/healthz`.
5. For each agent CLI it finds, asks before registering Jesinia with it (see
   [Agent registration](#agent-registration)).
6. Offers a `jesinia` command in `~/.local/bin` (add that folder to your `PATH` if it
   isn't already).
7. Prints a one-time login link.

Setup is safe to run again. It re-renders the LaunchAgent, restarts the server and
refreshes registrations without changing your token or data.

## Signing in

Open the link that setup printed. It holds a single-use nonce that expires in a few
minutes. The browser then keeps an `HttpOnly` session cookie; the token itself never
appears in a URL. For a new link at any time, run:

```bash
jesinia status
```

In Chrome or Edge, use **Install** in the address bar to give Jesinia (the launcher) and
Notes their own app windows.

## Everyday commands

| Command | What it does |
|---|---|
| `jesinia status` | Server health per module, and a fresh login link |
| `jesinia doctor` | Checks config, KB, git remote, tools, agent logins, the LaunchAgent and the server, with a fix for each problem. Exits 1 on any `fail` |
| `jesinia sync now` | Runs a KB git sync cycle now |
| `jesinia reindex` | Rescans the KB and rebuilds search (with the server stopped, it deletes the derived databases instead, and the next start rebuilds them) |
| `jesinia backup [--dest DIR]` | Copies agent history and `config.toml` to a timestamped folder |
| `jesinia config get KEY` / `set KEY VALUE` | `server.port`, `kb.path`, `kbsync.interval` (e.g. `1h`), `kbsync.quiet`, `folders.note`, `folders.source` (kept RSS posts, default `Sources`), `rss.retention` (1–365 whole days, e.g. `30d`) |
| `jesinia token show` / `rotate` | Show the install token, or replace it (signs browsers out and re-registers agents) |
| `jesinia update` | One update attempt (what the update agent runs) |
| `jesinia openapi` | Prints the HTTP API schema |

Most settings are also in the Settings app (`/settings/`). Changing `server.port` needs
a restart: `./setup` again, or
`launchctl kickstart -k gui/$(id -u)/com.jesinia.server`.

## Where things live

| What | Where |
|---|---|
| Config (mode 600) | `~/Library/Application Support/jesinia/config.toml` |
| Databases | `~/Library/Application Support/jesinia/*.sqlite`: `sessions` is history (backed up); the rest are rebuildable |
| Agent session folders | `~/Library/Application Support/jesinia/sessions/<id>/` |
| Server log (JSON lines, 10 MB × 5) | `~/Library/Logs/jesinia/server.log` |
| launchd output | `~/Library/Logs/jesinia/launchd.{out,err}.log` |
| Update log | `~/Library/Logs/jesinia/update.log` |
| LaunchAgents | `~/Library/LaunchAgents/com.jesinia.server.plist`, `com.jesinia.updater.plist` |
| Your KB | wherever `kb.path` points; Jesinia never moves it |

Set `JESINIA_HOME` to relocate the config, data and logs, for example for a test
install.

## Updating

- **Automatic** (`--auto-update`): the `com.jesinia.updater` agent runs `jesinia update`
  at login and every 15 minutes. It only acts when the checkout is on `main`, has no
  local changes, and `origin/main` is a fast-forward. It then:
  1. pulls;
  2. runs `make install build`;
  3. restarts the server and waits for `/healthz`.

  If the build or the restart fails, it resets to the previous commit, rebuilds and
  restarts, so a bad commit never leaves you without a server. A restart only counts as
  healthy when `/healthz` reports every required module ok (`agents` and `github` are
  optional), the same check `jesinia install` uses. The list of required modules comes
  from the new version itself, so a commit that renames or removes a module isn't rolled
  back for it; if the new version can't list them, every non-optional module its
  `/healthz` reports must be ok. Each attempt adds a line to `update.log`.
- **SSH remotes:** the update agent doesn't store an `SSH_AUTH_SOCK` path (it changes at
  every login). If it doesn't inherit one, `jesinia update` asks launchd
  (`launchctl getenv SSH_AUTH_SOCK`) before fetching. Without an agent socket, an SSH
  fetch fails and the attempt is skipped; an HTTPS remote needs none.
- **What you're trusting:** auto-update runs whatever is pushed to `main` on the remote
  you cloned from, just like `git pull && ./setup`. The remote URL is pinned when you
  enable auto-update (so the checkout must have an `origin` remote; without one,
  `--auto-update` fails and asks you to add it), and if `origin` is changed, updates stop
  until you run
  `jesinia install --kb-path <kb> --auto-update` again. Protect `main` on that remote if
  others can push to it.
- **Manual:** `cd ~/apps/jesinia && git pull && ./setup`.
- Turn auto-update on or off later with `jesinia install --kb-path <kb> --auto-update`
  or `--no-auto-update`. Running `./setup` without the flag leaves it as it is.

## Agent registration

`jesinia install` asks before registering with each agent CLI it finds (`--yes` skips
the question). Registrations carry the install token, so `jesinia token rotate`
re-registers every agent that has one. The commands were checked against Claude Code
2.1.301 and Codex 0.163.0.

| Agent | Registered | Removed by uninstall |
|---|---|---|
| Claude Code | MCP: `claude mcp add --scope user --transport http jesinia http://127.0.0.1:5374/mcp --header "Authorization: Bearer <token>"`. Skills: `claude plugin marketplace add <checkout>/skills/claude-code` and `claude plugin install jesinia@jesinia --scope user` | `claude mcp remove --scope user jesinia`, `claude plugin uninstall jesinia@jesinia`, `claude plugin marketplace remove jesinia` |
| Codex | MCP: `codex mcp add jesinia --url http://127.0.0.1:5374/mcp`, plus a `[mcp_servers.jesinia.http_headers]` table with `Authorization = "Bearer <token>"` in `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`). Instructions: a managed block (between `<!-- jesinia:begin` and `<!-- jesinia:end -->`) in `~/.codex/AGENTS.md` | `codex mcp remove jesinia` (which also drops the header table), and the managed block is removed |

`codex mcp add` has no header flag. `--bearer-token-env-var` would need `JESINIA_TOKEN`
in every shell that starts Codex, so the static header in Codex's own config is used
instead. The token in each agent's config grants loopback-only access to this Mac's
server.

Sessions that Jesinia launches itself don't use these registrations. Each gets its own
token through `--mcp-config` (Claude Code) or `-c mcp_servers.jesinia.*` (Codex), with
a tool allowlist ([agents](conventions/agents.md)).

After registering, try it in a terminal: ask Claude Code or Codex to "search my Jesinia
KB for …" (`kb_search`). In Claude Code, `/jesinia-note <topic>` drafts a note.

## Uninstall

```bash
cd ~/apps/jesinia
./setup --uninstall                # keeps data and the KB
./setup --uninstall --purge-data   # also deletes config, databases and logs (asks first)
```

Uninstall stops and removes both LaunchAgents, the agent registrations and the
`jesinia` command. **It never touches your KB folder.** If the KB happens to live inside
the data folder, `--purge-data` refuses, so remove those by hand. Afterwards you can
delete the checkout (`rm -rf ~/apps/jesinia`).

## Troubleshooting

Start with `jesinia doctor`. Each line is `ok`, `warn` or `fail`, with a fix after `→`.

### The KB folder is missing or unwritable

A red **KB unavailable** banner shows in every app, and `doctor` reports
`fail kb.path`. Point Jesinia at the right folder:

```bash
jesinia config set kb.path ~/path/to/kb   # or Settings → Knowledge base → Save
```

From Settings the change applies immediately and the KB is reindexed. From the CLI,
restart the server (`launchctl kickstart -k gui/$(id -u)/com.jesinia.server`).

### Sync paused: merge conflict

The KB's git pull hit a conflict, so Jesinia paused sync and left your files as they
were. Resolve it in the KB:

```bash
cd ~/path/to/kb
git pull --rebase           # shows the conflicted files
# edit each conflicted file and remove the <<<<<<< ======= >>>>>>> markers
git add <file>...
git rebase --continue       # repeat until it finishes
git push
```

Then choose **Resume** in the banner or in Settings → Git sync. See
[KB sync](conventions/kb-sync.md) for the whole cycle.

### An agent is unavailable or logged out

Notes shows "No agent is available", or Settings → Agents says *Unavailable*:

- Claude Code: run `claude` once and log in (or `claude login`).
- Codex: run `codex login`.

Then choose **Check again** in Settings. The server finds the CLIs through the `PATH` it
was installed with, so if you installed an agent later, run `./setup` again.

### The server isn't running

- `jesinia status` says it isn't running: check `~/Library/Logs/jesinia/launchd.err.log`
  and `server.log`.
- **Port in use:** another program has 5374. Pick another port with
  `jesinia config set server.port 5375`, then run `./setup` again (it re-registers agents
  with the new URL).
- `launchctl print gui/$(id -u)/com.jesinia.server` shows the agent's state, last exit
  code and PID.

### The login link doesn't work

Links are single-use and expire after a few minutes. Run `jesinia status` for a new
one. If every request says "Signed out", the token was rotated: open a new link.

### An update failed

`~/Library/Logs/jesinia/update.log` says why. Common causes:

- local changes in the checkout (`git status`);
- a branch other than `main`;
- a build failure, which is rolled back automatically.

`jesinia update` runs one attempt in the foreground.

## Developing alongside an install

Work in a separate clone, not in `~/apps/jesinia` (the update agent skips a dirty
checkout, but a clean one is easier to trust). For example:

```bash
git clone --recurse-submodules https://github.com/dalbrecht/jesinia.git ~/projects/jesinia
cd ~/projects/jesinia
make install
make ci                       # everything CI runs
make test-e2e                 # Playwright, against a throwaway server
JESINIA_HOME=/tmp/jx uv run --project server jesinia config set server.port 5380
JESINIA_HOME=/tmp/jx uv run --project server jesinia serve   # a second server for testing
```

`make help` lists every target; the Makefile is the only entry point.
