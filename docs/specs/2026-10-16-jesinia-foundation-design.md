# Jesinia — Foundation Design

- **Status:** Draft for review
- **Date:** 2026-10-16
- **Author:** Don Albrecht (with Claude)
- **License of this project:** AGPL-3.0-or-later
- **Scope:** the foundation sub-project only. The three products (RSS reader, code
  review tool, GitHub issues GUI) each get their own spec → plan → build cycle after
  this one lands (see §12).

## 1. Purpose

Jesinia is a personal-development project to prototype and improve a **personal
knowledge base (KB)** and the local AI-assisted tools that feed it. It is:

1. A set of **conventions docs** describing how everything works and interacts — the
   thing that keeps momentum and makes the project shareable.
2. A collection of **AI agent skills/plugins** so locally running agents (Claude Code,
   Codex) work with the KB and with Jesinia correctly.
3. A **local Python server**, installed on the user's Mac via launchd, that serves PWAs,
   provides services to them, and mediates between the user's local AI agents, Jesinia's
   services, and the KB on the local filesystem.
4. A **file-first KB**: Markdown and other text files on disk are the source of truth.
   Jesinia keeps its own SQLite databases for richer metadata, indexes, and cached
   remote-API content.

### Success criteria for the foundation

The foundation is done when, on a fresh Mac:

- `./setup` installs Jesinia and leaves a running launchd-managed server.
- The KB path is set, the KB is indexed and searchable, and KB git sync works.
- The MCP endpoint is usable from Claude Code and from Codex.
- A PWA chat pane runs a headless agent session that reads the KB and writes a page in
  the correct format (proven by the **Notes tracer app**, §11).
- The conventions docs set (§10) exists.
- `make ci` is green in GitHub Actions.

### Non-goals (foundation)

- The RSS reader, code review tool, and issues GUI themselves (separate specs).
- Packaging (Homebrew/pipx), auto-update, multi-machine or multi-user operation.
- Direct LLM API calls with stored API keys (agents are driven through their CLIs).
- Sharing code with, or reading the databases of, the Pigeonpile suite.
- The final visual identity of the design system (scaffold only, §9).

## 2. Context and constraints

- **Audience:** built for the author's Mac first, in a **public** GitHub repo
  (`dalbrecht/jesinia`). Setup and docs assume a stranger is reading.
- **License:** AGPL-3.0-or-later. Every source file carries an SPDX header.
- **Pigeonpile interop is format-only.** The Pigeonpile suite (`amicus-platform`) is
  proprietary to C65 LLC; Jesinia shares **no code** with it and never touches its
  databases. Integration means Jesinia writes KB files in the suite's corpus file format
  so the Pigeonpile Wiki can open the same folder. Jesinia's docs restate the format
  independently (§5).
- **Design language:** Jesinia has its own design system (`@jesinia/design`, §9); it
  does not use the proprietary Daylight/amicus tokens.
- **Coding standards:** [c65llc/coding-standards](https://github.com/c65llc/coding-standards)
  (MIT) is the authority for tooling, layering, coverage, and automation (§8).

## 3. Architecture

**Approach:** one server process; apps are internal modules; cross-cutting capabilities
are **shared services**.

### 3.1 Repository layout

```
jesinia/
  server/                    Python package (uv)
    jesinia/
      core/                  config, paths, auth tokens, logging, event bus, db helpers, scheduler
      services/              shared services — the only cross-module surface
        kb/                  read/write/move docs in corpus format; ids; watcher
        search/              FTS5 index over KB (+ app-registered records)
        agents/              headless agent runners, sessions, SSE streaming
        kbsync/              periodic git commit + push of the KB
        github/              gh-authenticated client + response cache
        fetch/               HTTP fetch + remote-content cache
      apps/
        notes/               foundation tracer app (§11)
        (rss/, review/, issues/ — later specs)
      mcp/                   single MCP endpoint aggregating service + app tools
      cli/                   `jesinia` CLI
    tests/
  web/                       pnpm workspace
    packages/design/         @jesinia/design — tokens, reset, components
    packages/client/         @jesinia/client — typed API client + SSE helpers
    apps/launcher/           launcher + settings PWA
    apps/notes/              tracer PWA
  skills/                    Claude Code plugin + Codex AGENTS.md snippet + MCP configs
  launchd/                   LaunchAgent plist template
  docs/                      conventions/, adr/, specs/, operations.md
  setup                      install/uninstall script
  Makefile                   the only entry point for build/test/lint (§8)
  .standards/                coding-standards submodule
  LICENSE                    AGPL-3.0-or-later
```

### 3.2 Dependency rules

- `core` ← `services` ← `apps`. Apps may import services; services never import apps;
  **apps never import each other**.
- When an app needs something another app built, that capability is **promoted into
  `services/`** with its own interface and tests — never imported sideways.
- Inside every service and app, Clean Architecture layers per the coding standards:
  `domain/` ← `application/` ← `infrastructure/`, plus `api/` (HTTP routes + MCP tools)
  as the outermost adapter.
- Both rule sets are enforced in CI with **import-linter** contracts.

### 3.3 Service contract

Each service exposes three faces over **one** application-layer implementation:

1. A Python interface (a `Protocol` + implementation) for in-process callers.
2. HTTP routes under `/api/<service>/…` for PWAs.
3. MCP tools named `<service>.<verb>` for agents. This spec writes names with a dot for
   readability; the **wire name** uses an underscore (`kb_search`), because LLM tool names
   must match `^[a-zA-Z0-9_-]{1,64}$`.

The PWAs, other apps, and terminal agents therefore all exercise the same code path.

### 3.4 Data

- One SQLite file **per service and per app** under
  `~/Library/Application Support/jesinia/`, each with its own migration history
  (sequential numbered SQL migrations, a `schema_version` table per database).
- **Rebuild rule:** every database is one of three classes, declared in its module:
  - *derived* (e.g. `search`, `notes`) — reconstructible from the KB files plus remote
    APIs; `jesinia reindex` performs the rebuild.
  - *cache* (`fetch`, `github`) — disposable and size/age-bounded.
  - *history* (`sessions`) — agent transcripts, not reconstructible. Loss is tolerable
    (anything worth keeping was saved to the KB), and history databases are included in
    `jesinia backup`.

## 4. Shared services (foundation)

### 4.1 `kb`

- Resolves the configured KB root; reads, creates, updates, and moves pages following
  §5. Mints UUIDv7 `id`s.
- All writes are atomic (write temp file in same directory → `fsync` → rename).
- Emits `kb.page_changed {id, path, change}` events on the core event bus for every
  Jesinia write.
- A filesystem watcher (macOS FSEvents via `watchfiles`) detects external edits
  (Pigeonpile Wiki, editors, `git pull`) and emits the same events.
- Adoption of plain `.md` files (§5.4) happens on scan and on watcher events.
- MCP tools: `kb.search` (delegates to `search`), `kb.read`, `kb.write`, `kb.move`,
  `kb.link` (resolve `[[id|Title]]`), `kb.sync_status` (delegates to `kbsync`).

### 4.2 `search`

- Owns `search.sqlite` with an FTS5 index over KB pages (title, body, `jesinia:` fields,
  path) kept current from `kb.page_changed` events.
- Apps may register **non-KB records** (e.g. unread RSS items) tagged by `source`, so one
  query can span KB and app content. Results carry `source` and `authoritative`
  (true only for KB pages).
- Full rebuild on `jesinia reindex` or when the KB path changes.

### 4.3 `agents` (agent bridge)

Two directions, one service.

**Jesinia → agent (PWA-first chat).**

- `AgentRunner` protocol with adapters:
  - `ClaudeCodeRunner` — `claude -p --output-format stream-json`, resuming the agent's
    session id for follow-up turns.
  - `CodexRunner` — `codex exec --json`, resuming likewise.
- Runners use the CLI's **existing login**; Jesinia stores no API keys. Runner
  availability is detected at startup and reported in Settings and `jesinia doctor`.
- **Sessions** (`sessions.sqlite`): a session binds a *subject* (e.g. a KB page, later an
  RSS item, PR, or issue), the runner, the agent-side session id, and the transcript.
  Tokens and tool events stream to the PWA over SSE.
- **Context packs:** the owning app assembles a pack (subject content, related KB pages
  from `search`, metadata) written as files into a per-session scratch directory
  (`~/Library/Application Support/jesinia/sessions/<id>/`), plus a short role prompt
  supplied by the app.
- **Agent tool policy for Jesinia-launched sessions:**
  - Working directory is the session scratch dir.
  - Jesinia's MCP server is attached; KB access goes through `kb.*` tools only.
  - The agent's built-in file-write and shell tools are disabled or restricted by an
    explicit allowlist passed on the CLI.
  - Write-type MCP tools require confirmation in the PWA unless the user's triggering
    action *is* that write (e.g. "Save note").
- **Untrusted content:** any remote content (and any KB page content when used as
  context) is placed in the pack inside clearly labeled untrusted-data blocks; role
  prompts instruct agents to treat it as data, never instructions.

**Agent → Jesinia (terminal agents).**

- One MCP endpoint, `http://127.0.0.1:<port>/mcp` (streamable HTTP), bearer-token
  authenticated (§7.4).
- Tool names are namespaced by owner (`kb.*`, `search.*`, `agents.*`, `notes.*`; later
  `rss.*`, `review.*`, `issues.*`).
- `agents.session_attach <id>` lets a Claude Code or Codex session in the terminal pick up
  a PWA-started session: it receives the same context pack and transcript, and its
  activity is appended to the session so the PWA shows it live.

### 4.4 `kbsync`

**Settings.** `kb.path` lives in `config.toml` and is editable from the Settings PWA,
`jesinia config set kb.path <dir>`, and `./setup`. Changing it validates the directory
(exists, writable), restarts the watcher, re-runs git detection, and triggers a full
reindex.

**Auto-detection** (on path change and on every server start):

| Found in KB folder | Behavior |
|---|---|
| `.git` with a remote | Periodic commit **and push** enabled automatically |
| `.git`, no remote | Periodic commit only; Settings shows "add a remote to enable push" |
| no `.git` | Sync off; Settings offers an "Initialize git" button (never automatic) |

**Cycle** — default interval **4 hours** (`kbsync.interval`, configurable); also runs on
server shutdown and on "Sync now" (`jesinia sync now`, Settings button,
`kb.sync_now` MCP tool):

1. If the working tree is clean **and** the branch is not ahead of its upstream: **skip
   entirely** — no commit, no fetch, no network.
2. Wait until the KB has been quiet for 30 s (no watcher events), to avoid committing
   half-written files.
3. `git add -A` (honoring the KB's own `.gitignore`); commit with a generated summary
   message (e.g. `kb: 3 updated, 1 added (Sources/…)`) and a `Jesinia-Auto-Sync: true`
   trailer.
4. If push is enabled: `git pull --rebase --autostash` from the tracked upstream.
5. On rebase conflict: `git rebase --abort` (working tree restored), **pause auto-sync**,
   raise a banner in all PWAs and a warning in `kb.sync_status`. Never force-push, never
   auto-resolve. Sync resumes after the user resolves and clicks "Resume".
6. Otherwise `git push`. Files changed by the pull flow back through the watcher into
   reindexing.

**Guards.** Uses the user's existing git credentials (ssh-agent / keychain helper); the
LaunchAgent environment carries `SSH_AUTH_SOCK`. `./setup` runs `git ls-remote` against
the upstream and warns if it fails. Failures back off exponentially (capped at the
interval). Status — last run, last result, last error, ahead/behind — is shown in
Settings and returned by `kb.sync_status`.

### 4.5 `github`

- Authenticates via the `gh` CLI's token (`gh auth token`); a fine-grained PAT in the
  macOS Keychain is the fallback. Jesinia never writes tokens to disk.
- Thin client over REST/GraphQL with ETag-aware response caching in `github.sqlite`.
- The foundation ships only auth, client, and cache — used by the later review and issues
  specs. Health appears in `jesinia doctor`.

### 4.6 `fetch`

- HTTP fetch with conditional requests (ETag/Last-Modified), per-host rate limits, size
  caps, and a content cache in `fetch.sqlite`.
- HTML sanitization helper (allowlist-based) for anything that will be rendered.
- Used by the Notes tracer only lightly; exists for the RSS spec.

## 5. KB on-disk conventions

These rules match the Pigeonpile suite's corpus file format so the Pigeonpile Wiki and
Jesinia can share a KB folder. Jesinia's `docs/conventions/kb-format.md` is the
canonical statement for this project.

### 5.1 Paths and identity

- **Title-as-path**, spaces preserved; nested directories mirror tree depth:
  `Reading/Feeds/Some Post Title.md`.
- **Identity is the front-matter `id`** (UUID). The path is a display artifact; Jesinia
  matches files by `id`, so renames and moves are treated as moves, never
  delete + create.
- Title sanitization for disk (path separators, control characters, trailing dots or
  spaces; empty → `Untitled`) may alter the filename without affecting identity.
- Same-titled siblings (case-insensitive) suffix all but the id-ordered first with
  ` [<id-prefix>]` — never a bare ` 2`.
- **Reserved top-level names:** `Journal/` and `Drafts/` belong to the Pigeonpile suite;
  Jesinia never writes inside them.

### 5.2 Front-matter

```yaml
---
id: 0192f7a8-…
title: Some Post Title
jesinia:
  kind: source/rss          # note | source/rss | review/pr | …
  source: https://…         # canonical remote URL, when there is one
  captured: 2026-10-16T09:12:00-07:00
---
```

- `id` and `title` are the shared fields. The title line is emitted unquoted only when it
  round-trips as a bare YAML scalar; otherwise JSON-quoted.
- Jesinia's additions live **only** under the single namespaced `jesinia:` key and hold
  only what is needed to reconstruct the page's meaning. Everything derived (read state,
  scores, transcripts, embeddings) lives in SQLite keyed by page `id`.

### 5.3 Body conventions

- Internal links: `[[id|Title]]` (or `[[id]]`). Tags: `#hashtags` in the body.
- **AI provenance uses Markdown (HTML) comments**, not headings or labels:

  ```markdown
  <!-- jesinia:ai agent=claude-code model=claude-opus-5-5 at=2026-10-16T09:20Z session=01J… -->
  …agent-written text…
  <!-- /jesinia:ai -->
  ```

  Prose reads naturally; the user may freely edit or merge AI text into their own. The
  comments are advisory provenance, not enforced boundaries; drift is acceptable.
- Assets (images saved from sources) go in `_assets/<page-id>/` and are referenced by
  relative path. Non-markdown files are ignored by the suite's tree.

### 5.4 What counts as a page

- `.md` with front-matter containing `id` → a page.
- Plain `.md` with no front-matter → **adopted**: an `id` is minted, the title is taken
  from the filename, and front-matter is written back.
- Front-matter **without** `id` (Jekyll etc.) → ignored, never rewritten.
- Non-markdown files → ignored. Paths with a dotfile segment → hidden.
- Duplicate files claiming one `id` (sync-tool copies) → surfaced as a conflict in
  Settings; Jesinia does not auto-delete either copy.
- **No delete originates from a vanished file.** A missing file is reported, not
  propagated; deletes happen explicitly (Jesinia UI/MCP or the suite).

### 5.5 Default folders

Configurable per kind: `Notes/` (tracer app), later `Sources/` (engaged RSS posts) and
`Reviews/` (PR reviews). Everything else stays wherever the user puts it.

### 5.6 Compatibility verification (first foundation task)

Two behaviors of the Pigeonpile Wiki are unverified and are tested first with a
round-trip fixture (Jesinia writes a page → Wiki opens and re-saves it → Jesinia
re-reads):

1. **Unknown front-matter keys** — does the Wiki preserve the `jesinia:` block on
   rewrite? If not, fallback: keep the block only in SQLite plus a rebuildable sidecar
   export at `<kb>/.jesinia/meta/<id>.yaml` (dot-path, so hidden from the suite).
2. **HTML comments** — does the Wiki's renderer hide `<!-- … -->`, or escape it to
   visible text? If escaped, raise it as a suite-side issue and choose an alternative
   marker before the RSS spec relies on provenance.

Results are recorded in an ADR.

## 6. Server runtime

- Python ≥ 3.12, `uv`-managed; FastAPI + uvicorn; single process.
- Background jobs (KB sync, reindex, later RSS polling) run in-process on an asyncio
  scheduler in `core`; no separate worker daemons.
- Static PWA bundles are built into `server/jesinia/static/<app>/` and served at
  `/<app>/`; `/` serves the launcher; `/settings/` the settings view; `/design/` the
  design-system demo page.
- **`jesinia` CLI** (Typer): `serve`, `status`, `doctor`, `config get|set`,
  `token show|rotate`, `sync now`, `reindex`, `backup`.
- Logs: structured (JSON lines) to `~/Library/Logs/jesinia/server.log`, rotated.
- `GET /healthz` reports process health plus per-service status.

## 7. Install, launchd, and security

### 7.1 launchd

- Per-user LaunchAgent `com.jesinia.server` at `~/Library/LaunchAgents/`, generated from
  `launchd/com.jesinia.server.plist.template` with absolute paths to `uv` and the repo.
- `RunAtLoad: true`; `KeepAlive: {SuccessfulExit: false}` (restart on crash only);
  `EnvironmentVariables` include `PATH` (with Homebrew and agent-CLI locations detected
  at setup) and `SSH_AUTH_SOCK`.
- Loaded with `launchctl bootstrap gui/$UID …`; unloaded with `launchctl bootout`.

### 7.2 `./setup`

Idempotent and rerunnable; supports `--dry-run` and `--uninstall`.

1. Check prerequisites and print exact remedies: macOS, git, `uv`, Node + pnpm (via
   Corepack), `gh`; optional `claude` and `codex` CLIs.
2. `make install` (→ `uv sync`, `pnpm install`) and `make build`.
3. Prompt for the KB path; show the git auto-detection result (§4.4) and run the
   `git ls-remote` check when a remote exists.
4. Create the data directories, `config.toml` (mode `0600`), and the install token.
5. Install and bootstrap the LaunchAgent; wait for `/healthz`.
6. For each detected agent CLI, **ask** before registering the MCP server and installing
   the skills plugin (§4.3, §10).
7. Print the launcher URL.

`--uninstall` boots out and removes the LaunchAgent and agent registrations, and asks
before removing data directories. It never touches the KB.

### 7.3 Network

- Bind `127.0.0.1` only (configurable port, default `4870`).
- Reject requests whose `Host` or `Origin` is not the local origin (DNS-rebinding
  defense).
- Strict CSP: `default-src 'self'`; no inline scripts. Remote HTML is sanitized
  server-side and rendered in a sandboxed iframe.

### 7.4 Authentication

- An install token (256-bit random) in `config.toml`.
- PWAs: `./setup` and `jesinia status` print a one-time launcher URL containing a
  short-lived login nonce; visiting it sets an `HttpOnly; SameSite=Strict` session
  cookie. The token itself never appears in a URL.
- MCP clients and the CLI send `Authorization: Bearer <token>`.
- `jesinia token rotate` invalidates sessions and re-registers MCP configs.

### 7.5 Agent and content safety

- Jesinia never executes shell commands derived from remote or KB content.
- Agent sessions are confined as in §4.3 (scratch dir, MCP-only KB access, tool
  allowlist, confirmation on writes).
- Jesinia stores no credentials of its own: GitHub via `gh`/Keychain, agents via their
  own logins, git via the user's credential helper.

## 8. Engineering standards and tooling

The c65llc coding standards are authoritative; this section names how Jesinia adopts
them, not a restatement.

- `.standards/` is a git submodule plus `.standards.yml` created by the standards
  installer: languages `python`, `typescript`; agents `claude-code`, `codex`, `copilot`.
  The installer generates `CLAUDE.md`, `AGENTS.md`, and Copilot instructions;
  `make sync-standards` and `make doctor` keep them current.
- **Tooling is whatever the standards specify** at sync time. Currently: Python — `uv`
  (lockfile committed), `ruff format` / `ruff check` (line length 100), `mypy --strict`,
  `pytest` + `pytest-cov`; TypeScript — `pnpm` pinned via Corepack, `prettier` (line
  length 120), `eslint` + `@typescript-eslint` + `eslint-plugin-security`, `vitest` +
  `@testing-library`, Playwright for e2e.
- **The Makefile is the only entry point**; CI and contributors never call `uv`/`pnpm`
  directly. Targets include `install`, `build`, `test`, `test-e2e`, `lint`, `fmt`,
  `fmt-check`, `coverage-check`, `hooks`, `ci-py`, `ci-js`, and `ci` (both). The Python
  and JS toolchains stay separate (neither orchestrates the other).
- **Coverage:** ≥ 95% per module; domain targeted at 100%.
- **Layering** enforced by import-linter (§3.2).
- **TDD** for all domain and application code.
- **CI:** GitHub Actions on PRs runs `make ci` (including `make lint-standards`) plus the
  opt-in standards-review workflow. macOS runner for launchd/setup smoke tests; Linux for
  the rest.
- **Workflow:** one task = one branch = one PR; each PR gets a local code review, CI,
  and GitHub Copilot review, and all findings are addressed before merge.
- **Licensing:** `LICENSE` (AGPL-3.0-or-later) and `SPDX-License-Identifier:
  AGPL-3.0-or-later` headers in every source file, checked in CI.

## 9. Design system (`@jesinia/design`)

The foundation builds the **scaffold**; the visual identity (palette, type, voice) is a
separate design pass that swaps values behind the tokens without touching apps.

- Three token tiers: primitive palette → semantic roles → component tokens. Light and
  dark themes via `prefers-color-scheme`, overridable with `[data-theme]`.
- Reset, type scale, spacing scale, radius/shadow geometry.
- Core React components: Button, Input, Panel, List/Row, Badge, Tabs, Toast, and a Chat
  transcript (streaming-aware), since all three products need chat.
- Placeholder values are deliberately neutral.
- A demo page at `/design/` renders every token and component in both themes.
- Accessibility: WCAG 2.2 AA contrast for semantic color pairs (checked in tests),
  keyboard operability, visible focus.

## 10. Documentation, skills, and plugins

### 10.1 Docs (`docs/`)

- `README.md` — what Jesinia is, 5-minute quickstart, license.
- `docs/conventions/` — the "how everything works" set, cited by the agent skills:
  - `kb-format.md` — §5 in full, including a Pigeonpile-compatibility section.
  - `architecture.md` — layering, service contract, promoting code to a service, one DB
    per module, rebuild rule.
  - `adding-an-app.md` — checklist: routes, DB + migrations, MCP tools, PWA, skill, docs.
  - `agents.md` — runners, sessions, context packs, MCP naming, untrusted-content
    handling, what skills must teach.
  - `kb-sync.md` — detection, cycle, conflict recovery steps.
  - `security.md` — one-page threat model.
  - `design-system.md` — tokens, components, usage rules.
- `docs/adr/` — initial ADRs: AGPL license; single-server / internal-apps / shared-services
  architecture; format-only Pigeonpile interop; files as source of truth; CLI-driven
  agents (no stored API keys); KB git sync policy; and the §5.6 compatibility results.
- `docs/specs/` — this spec, then one per product.
- `docs/operations.md` — setup, uninstall, logs, `doctor`, upgrade
  (`git pull && ./setup`), troubleshooting.

### 10.2 Skills and plugins (`skills/`)

- **Claude Code plugin** bundling the MCP server config, skills, and slash commands.
  Foundation skills:
  - `jesinia-kb-conventions` — how to read/write the KB correctly (via `kb.*` tools,
    provenance comments, links, folders).
  - `jesinia-notes` — start or attach to a Notes session on a KB page.
  Later specs add `engage-post`, `review-pr`, `triage-issues`.
- **Codex:** an `AGENTS.md` snippet carrying the same conventions plus MCP config.
- Skills reference `docs/conventions/` as their source of truth and are tested by a
  smoke script that runs each agent CLI headlessly against a fixture KB (skipped in CI
  when the CLI is unavailable).

## 11. Notes tracer app

A minimal app that exercises every foundation service end to end before product work
starts.

- **UI (`/notes/`):** search the KB (`search`), open a page (`kb.read`), start a chat
  session about it with the chosen runner (`agents`, streamed via SSE), and "Save note"
  — which writes a new or updated page in `Notes/` with correct front-matter, a
  `[[id|Title]]` back-link to the subject page, and `jesinia:ai` provenance comments
  around agent-written text (`kb.write`).
- **Terminal path:** `agents.session_attach` from Claude Code continues the same session;
  the PWA shows its activity.
- **Data:** `notes.sqlite` holds only session ↔ page associations (rebuildable from
  front-matter and sessions).
- **e2e test:** Playwright drives the flow with a fake `AgentRunner` that emits a
  scripted stream (CI-safe); a manual check covers the real CLIs.

## 12. Roadmap after the foundation

Each is its own spec → plan → PRs:

1. **RSS reader** — feeds/OPML, polling (`fetch`), reader UI, **Engage**: discuss the
   post with the user's agent using it as context, then save to `Sources/` as an
   augmented KB page.
2. **Code review tool** (GitHub) — background pack on PR, codebase, and domain (from
   KB); narrative walkthrough of the change; scoring against user-defined metrics; a
   comprehension quiz for the reviewer; an approve/reject **recommendation** that is
   advisory and never posts to GitHub without an explicit click.
3. **GitHub issues GUI** — board/list/triage views across the user's repos, with
   agent-assisted triage.

## 13. Error handling summary

| Situation | Behavior |
|---|---|
| KB path missing/unwritable | Server starts degraded; KB-dependent features disabled; banner + `doctor` remedy |
| External edit mid-write | Atomic rename means readers see old or new, never partial; watcher reconciles |
| Duplicate `id`s on disk | Reported as a conflict; no auto-delete |
| Agent CLI missing / logged out | Runner marked unavailable; chat UI offers the other runner or shows the login command |
| Agent run fails mid-stream | Session marked failed with error; transcript so far kept; "Retry" resumes |
| Git push/pull failure | Exponential backoff; status surfaced; never force-push |
| Rebase conflict | Abort, pause sync, banner, user resolves |
| Migration failure | Server refuses to start that module, logs, `doctor` explains; other modules run |
| GitHub auth missing | `github` reports unavailable; dependent features disabled |

## 14. Testing strategy

- **Unit (pytest/vitest):** domain and application layers via TDD; ≥ 95% per module.
- **Integration:** services against a temp KB directory, temp SQLite files, and a local
  bare git repo as the "remote" for `kbsync` (including a forced rebase-conflict case).
- **Contract:** each service's HTTP routes and MCP tools are tested against the same
  application interface (one behavior, three faces).
- **Fake runner:** a scripted `AgentRunner` for deterministic streaming tests.
- **e2e (Playwright):** launcher, settings (KB path + sync status), and the Notes flow.
- **Setup smoke (macOS CI):** `./setup --dry-run`, then a real install into a temp HOME,
  `/healthz` check, `--uninstall`.
- **Compatibility fixture:** the §5.6 round-trip, run manually against the Pigeonpile Wiki
  and recorded in its ADR.
