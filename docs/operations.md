# Operations guide

Install, run, update and remove Jesinia on a Mac, and fix what goes wrong. For the short
version, see the [README](../README.md#quickstart).

- [Requirements](#requirements)
- [Install](#install)
- [Signing in](#signing-in)
- [Everyday commands](#everyday-commands)
- [Where things live](#where-things-live)
- [RSS](#rss)
- [Review](#review)
- [Issues](#issues)
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
| `gh` (needed for Review and Issues) | GitHub access for Review and Issues: Jesinia stores no credentials of its own and uses the token `gh` holds | `brew install gh && gh auth login` |
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
appears in a URL. The session survives server restarts and updates, lasts 30 days, and
renews while you use it. To sign in again, run:

```bash
jesinia login
```

It opens a fresh link in your default browser (`--print` prints it instead). Installed app
windows share the browser's cookies, so they are signed in too.

In Chrome or Edge, use **Install** in the address bar to give Jesinia (the launcher),
Notes and RSS their own app windows.

## Everyday commands

| Command | What it does |
|---|---|
| `jesinia status` | Server health per module, and a fresh login link |
| `jesinia login [--print]` | Opens a fresh login link in your browser (or prints it) |
| `jesinia doctor` | Checks config, KB, git remote, tools, agent logins, the LaunchAgent and the server, with a fix for each problem. Exits 1 on any `fail` |
| `jesinia sync now` | Runs a KB git sync cycle now |
| `jesinia reindex` | Rescans the KB and rebuilds search (with the server stopped, it deletes the derived databases instead, and the next start rebuilds them) |
| `jesinia backup [--dest DIR]` | Copies agent history, RSS feeds and read state, and `config.toml` to a timestamped folder |
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
| Databases | `~/Library/Application Support/jesinia/*.sqlite`: `sessions`, `rss` (feeds, posts and read state), `review` (rounds, drafts, posted reviews) and `issues` (queues, themes, drafts, batches) are history (backed up); the rest are rebuildable |
| Agent session folders | `~/Library/Application Support/jesinia/sessions/<id>/` |
| Server log (JSON lines, 10 MB × 5) | `~/Library/Logs/jesinia/server.log` |
| launchd output | `~/Library/Logs/jesinia/launchd.{out,err}.log` |
| Update log | `~/Library/Logs/jesinia/update.log` |
| LaunchAgents | `~/Library/LaunchAgents/com.jesinia.server.plist`, `com.jesinia.updater.plist` |
| Review's repo mirrors and checkouts | `~/Library/Application Support/jesinia/review/repos/<owner>/<name>/`; rebuilt by fetching |
| Your KB | wherever `kb.path` points; Jesinia never moves it |

Set `JESINIA_HOME` to relocate the config, data and logs, for example for a test
install.

## RSS

The RSS app (`/rss/`) reads your feeds; keeping a post writes it into the KB.

- **Adding feeds:** **+ Add feed** at the bottom of the feeds pane takes a feed URL or a
  site's address (the feed the site advertises is found). The same dialog files feeds into
  folders (one level), renames folders and unsubscribes. Unsubscribing drops the feed's
  unkept posts; kept posts stay. Adding the URL again restores the feed.
- **Polling:** every feed starts at 30 minutes and adapts between 15 minutes and 12 hours.
  **↻ Refresh** polls everything now. A feed that fails three times in a row shows its
  cause (any URL in it cut down to its host) with **Retry**, and keeps being retried with
  backoff; it is never dropped.
- **OPML:** import and export are in **Settings → RSS**. Import keeps one level of
  folders, skips feeds you already follow, and lists any entry whose URL can't be used.
  Imported feeds are read on the next poll (within a minute) or at once with
  **Refresh all feeds now**. Export downloads every feed and folder as
  `jesinia-feeds.opml` (also `GET /api/rss/opml`).
- **Retention:** unread and read posts you didn't keep are deleted once they are older than
  the retention (Settings → RSS, or `rss.retention`, 1–365 days, default 30). Kept posts
  are never deleted. Read state lives in `rss.sqlite`, never in KB files.
- **Kept posts** become pages in `Sources/` (Settings → RSS, or `folders.source`): your
  note, the tags, any Engage takeaway, then the archived article as Markdown. They sync
  with git like any page.
- **Archived images stay on this Mac.** They are saved under `_assets/<page-id>/` next to
  the page, and each of those folders has a `.gitignore` containing `*`, so KB sync never
  commits or pushes them. On another machine the page shows its text with broken image
  links; **Open original** still works. Deleting the folder loses only the images.
- **Agents** can find and read posts (`rss_search_posts`, `rss_get_post`) but never keep or
  mark them; see [agents](conventions/agents.md).

## Review

The Review app (`/review/`) is a queue of the pull requests that need you, a guided walkthrough
of each one, and AI findings that are ready before you look.

**Prerequisite: `gh auth login`.** Jesinia stores no GitHub credentials. It asks the `gh`
CLI for its token (`gh auth token`; a Keychain item named `jesinia-github` also works),
keeps it in memory for at most 5 minutes, and never writes or logs it. Git fetches use
your own git credential helper (on macOS, the Keychain). Without a token the queue shows
**GitHub auth missing** with the command to run; run `gh auth login`, and the next sync
picks it up (no restart). `/healthz` reports the `github` module as "no token: run `gh auth login`".

- **How it syncs:** every 2 minutes (and at startup) Jesinia refreshes the queue from GitHub
  into `review.sqlite`; the app always reads that cache and never waits for GitHub. It
  watches the repos you own or your orgs own that have a PR awaiting your review or written
  by you. Someone else's repo is only *suggested* until you **Confirm** it; nothing is
  fetched or pre-reviewed there before that. After a rate limit, Jesinia asks nothing until
  GitHub says the limit resets. A failed sync keeps the cached queue and says why.
- **Pre-reviews:** once CI passes and pushes have settled (10 minutes), Jesinia
  runs the correctness pass, findings and recommendation in the background (the default,
  *triage*; choose *everything* or *on open only* in **Settings → Review**, for every repo or
  per repo). Your agent (default Codex, or Claude Code, also in Settings → Review) reads the
  PR's checkout only through read-only repo tools. A PR you **Load** or open skips the wait.
  **Review anyway** goes ahead past a failing CI.
- **Settings → Review** (`/settings/#review`):
  - **Repos:** whether each is watched; its pre-review; its automated reviewer (a GitHub
    login such as `copilot`: Jesinia waits up to 30 minutes for its review on a new head);
    and its **metrics**, one criterion per line. Leave the metrics empty to let the standards
    decide what is scored. A change to a repo's metrics applies from its next review round:
    rounds already started keep the set they began with. Each save sends only what it
    changes, so a repo confirmed in the queue or edited in another tab is never put back.
  - **Standards:** your coding-standards checkout (the base layer) and an optional folder of
    your own standards documents (the last layer), each an absolute path or `~/…` (not `/`
    or your home folder itself). Only the `*.md` files directly in a folder are read;
    subfolders and symlinks are skipped.
  - **What Jesinia thinks you know:** each folder and topic Jesinia has a record of, how
    familiar it thinks you are, and why (your commits, the reviews you finished, quiz
    results). Mark one **I know this** or **I don't know this**, or clear the mark; a mark on a
    folder covers everything beneath it and the list says where an inherited mark comes from.
    **Show more** reads the next areas. KB pages aren't counted yet.
- **Kept reviews:** a deep review you post or mark **Done** is kept in your KB as
  `Reviews/<owner>-<repo>/#<n> <title>.md`, with its overview, the findings that mattered and
  your notes; keeping it again updates its section and keeps your edits. The PR view's last
  page says where it went, or why it couldn't be written, with **Retry** (**Keep it** before
  a first keep).
- **How it works:** each repo has a KB page, `Reviews/<owner>-<repo>/How it works.md`,
  describing its main parts. The first deep review drafts it and later approved deep reviews
  draft updates: nothing is written until you choose **Accept** (**Not now** leaves the page
  as it is). If you edited the page since the draft was made, the accept is refused and a
  fresh draft is made against your edit. Deep reviews read the page and flag PRs that
  contradict it as design findings.
- **Metrics trend:** a deep review's **Metrics** page ends with the repo's quality trend: one
  point per scored round, its mean score from 0 to 10, with the numbers in a table beneath.
- **Checkouts:** each watched repo gets a bare mirror under `review/repos/` and one checkout
  per reviewed head; unneeded ones are pruned daily. Jesinia never pushes.
- **Posting:** nothing reaches GitHub until you choose **Comment**, **Request changes** or
  **Approve**. The confirmation shows exactly what will be posted and every warning
  (BLOCKING text, an Approve against Jesinia's verdict, newer commits). It is sent as one
  review (`POST /repos/{owner}/{repo}/pulls/{n}/reviews`) with all inline comments, on the
  head you reviewed. The body ends with an invisible marker, so a lost response or a retry
  never posts twice: Jesinia looks for a review carrying the marker before it posts again.
  Agents can read the queue and findings (`review_queue`, `review_get`, `review_findings`)
  but have no way to post.
- **Backup:** `review.sqlite` is history, so `jesinia backup` includes it; the mirrors and
  checkouts are not (they are fetched again).

## Issues

The Issues app (`/issues/`) triages the issues of the repos you watch, against a label
convention per repo, with agent suggestions you approve. It groups issues into epics and
builds ordered work queues by drag and drop. It uses the same `gh` token as Review (see
[Review](#review)): without one the lists show **Signed out of GitHub** with the command to
run, and keep showing the cache.

- **Setup:** open **Settings → Issues**. **Suggestions runner** picks the agent that suggests
  labels, priorities, epics and queue orders. *Automatic* uses Claude Code when it is
  available, else the first available runner. Without a runner, suggestions wait and manual
  triage keeps working. The agent never changes GitHub: each suggestion is a draft you
  approve, and agents get only the read-only `issues_search`, `issues_get`, `queues_list` and
  `queue_get` tools.
- **Watching a repo:** type `owner/name` under **Add a repo** and choose **Watch**. Its open
  issues land in the **Backlog**, not the Inbox, and no suggestions run on them, so a big repo
  costs no agent time. Issues opened after that land in the **Inbox**, as does any issue that
  becomes incomplete. Sync runs every 5 minutes (and at start), reads changed issues only, and
  pauses until GitHub's reset after a rate limit. Unticking **Watched** stops the syncs and
  keeps the cache, queues and themes. Each save sends only what it changes, so two open tabs
  don't undo each other.
- **The label schema:** each repo's convention comes from its `.jesinia/labels.yml` when the
  file exists (read-only in Settings; it wins), else from the schema made in Settings, else from
  the default (readiness, priority, type, `area:` areas, `epic`, `initiative`). **Aliases** are
  existing labels that mean the same thing (for example `design needed` for `design-needed`).
  An alias counts toward completeness, and Jesinia writes the label name that exists on GitHub
  until the repo is migrated. **Labels on GitHub not in the schema** lists the repo's labels
  that count for nothing yet; **Alias of…** adds one, by its exact name, to the value it means.
  **Save labels** keeps the Settings schema (the previews wait until it is saved). **Use the
  default** clears it after a confirmation, with **Undo** to put it back. Label names are shown
  with any hidden or direction-changing character as a visible token.
- **Label setup:** Jesinia never creates a label implicitly. **Preview setup** lists the
  canonical labels the repo lacks; **Apply setup** creates exactly those, as your approval.
- **Label migration, and why it is safe:** **Preview migration** lists, for each alias on
  GitHub, the issues carrying it and the canonical label they move to. On **Apply migration**,
  Jesinia lists each old label's issues and pull requests again, live from GitHub (never from
  its cache, so an alias added since the last sync isn't stripped), moves each one, and deletes
  the old label only if every move succeeded. Otherwise the old label stays and the failed
  issues are named. Some steps are refused up front, with the reason shown in the preview:
  - a label whose name has a comma is skipped, because GitHub can't list issues by it: check
    it and delete it by hand;
  - two labels on GitHub that compare as one name (by case or Unicode form) refuse the
    preview: rename one on GitHub first.

  A plan is valid for 15 minutes and is refused if the repo's labels or schema changed since
  the preview: preview again. When GitHub stops a batch (a rate limit or a rejected token),
  the steps done so far are shown; preview again for what is left.
- **The Pending changes tray:** approving a suggestion applies it to GitHub at once. Every
  other GitHub change (a drag onto an epic or a board column, a picker, a menu action, the
  bulk bar, a queue's band move or Done) waits in the tray at the bottom of the window until **Apply**.
  Apply sends the whole tray as one batch: one ordered series per issue, checked against a
  fresh read. A step that GitHub changed underneath (a parent moved, a label already set) is
  shown as a conflict and never overwritten. A failure stops that issue's series and keeps what
  applied; **Retry** runs only the rest. Batches are kept 30 days, and an unfinished one is
  offered again after a reload. Queue order, themes and adding to a queue are local and apply
  at once; publishing a theme as a `theme:` label stages in the tray.
- **Backup:** `issues.sqlite` is history (queues, notes, themes and drafts are yours), so
  `jesinia backup` includes it; the issue cache is fetched again.
- **Acceptance (on the author's Mac):**
  1. watch a real repo (amicus-platform): its issues land in the Backlog, with no burst of
     agent runs;
  2. run its label setup, then map its labels as aliases (and migrate if wanted);
  3. triage five Inbox issues by approving suggestions;
  4. convert a checklist epic to sub-issues;
  5. build an "Agents: this week" queue by dragging, then **Copy brief**.

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

### Choosing the model

Settings → Agents has a **Model** field for Claude Code and for Codex. Each runner uses:

1. The model saved in that field, if there is one.
2. Otherwise `JESINIA_CLAUDE_MODEL` or `JESINIA_CODEX_MODEL` from the server's environment.
3. Otherwise the CLI's own default.

The field's note says which of these is in effect. Leave the field empty and choose
**Save** to go back to the environment variable or the default. A change applies from
each agent's next turn, with no restart. A model name is 1 to 100 of `A-Z a-z 0-9 . _ : -`
and can't start with `-`. The setting is stored in `sessions.sqlite`
(`GET` / `PATCH /api/agents/settings`).

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

### Review says "GitHub auth missing"

The Review queue shows a red **GitHub auth missing** banner, or `/healthz` reports
the `github` module as having no token. Jesinia stores no GitHub credentials; sign in with the GitHub CLI:

```bash
brew install gh        # if `gh` is missing
gh auth login
```

The next sync (within 2 minutes) picks the token up. If the server was installed before
`gh` was, run `./setup` again so launchd's `PATH` finds it.

### The server isn't running

- `jesinia status` says it isn't running: check `~/Library/Logs/jesinia/launchd.err.log`
  and `server.log`.
- **Port in use:** another program has 5374. Pick another port with
  `jesinia config set server.port 5375`, then run `./setup` again (it re-registers agents
  with the new URL).
- `launchctl print gui/$(id -u)/com.jesinia.server` shows the agent's state, last exit
  code and PID.

### The login link doesn't work

Links are single-use and expire after a few minutes. Run `jesinia login` for a new
one. If every request says "Signed out", the token was rotated or the session reached 30
days without use: run `jesinia login`.

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
