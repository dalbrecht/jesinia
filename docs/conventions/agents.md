# Agents

How Jesinia works with Claude Code and Codex (ADR 0005).

## Runners

| Runner | Command (abridged) | Confinement |
|---|---|---|
| `claude-code` | `claude -p --output-format stream-json --verbose --include-partial-messages --strict-mcp-config --mcp-config <session>/mcp.json --tools Read --setting-sources "" --disable-slash-commands --no-chrome` | Built-in tools cut to `Read`, allowed only inside the session folder, plus `mcp__jesinia__*`. Bash, Edit, Write, WebFetch and similar are disallowed |
| `codex` | `codex exec --sandbox read-only -C <session> [resume] --json --ignore-user-config --ignore-rules --disable shell_tool --disable apps … -c mcp_servers.jesinia.… [<id>] -` | Read-only sandbox, shell off, MCP tools allowlisted and pre-approved |

- Both use the CLI's own login, a minimal environment, and the prompt on stdin. A
  follow-up turn resumes the agent's session id.
- **Model:** each runner uses the model saved in Settings → Agents, else
  `JESINIA_CLAUDE_MODEL` or `JESINIA_CODEX_MODEL`, else the CLI's own default.
  - Runners ask for the model on every turn, so a change applies with no restart.
  - The settings live in the `settings` table of `sessions.sqlite`, under the key
    `model.<runner>`.
  - `GET /api/agents/settings` returns each runner's saved setting, its environment value
    and variable, the effective model and its source (`setting`, `environment` or
    `default`).
  - `PATCH /api/agents/settings` takes an `Idempotency-Key` header. It is a partial update:
    a runner left out keeps its model, and `null` clears it.
  - The name becomes one argv value (`-m` / `--model`), so it is validated at the boundary.
    It must be 1 to 100 of `[A-Za-z0-9._:-]` and can't start with `-`. Anything else gets
    422.
  - An unsafe environment value or stored row is ignored, and the runner falls back to the
    next source.
- Availability (`/api/agents/runners`) shows in Settings and in `jesinia doctor`.

## Isolation

Agents read untrusted text (PR bodies, diffs, feed posts, KB pages), so a session gets no
tool beyond Read on its own folder and Jesinia's MCP server. Neither CLI may load the
user's own MCP servers, plugins, connectors, hooks or skills. Auth still comes from the
CLI's normal login. Jesinia never edits the user's global Claude or Codex config.

- **Claude Code:** `--strict-mcp-config` loads only the session's `mcp.json`.
  `--tools Read` removes every other built-in tool (Artifact, Cron, Workflow, Skill,
  ToolSearch and so on). `--setting-sources ""` skips the user, project and local settings
  files, so their hooks, permission rules and enabled plugins (and those plugins' skills,
  agents and hooks) never load. `--safe-mode` and `--bare` don't work here: safe mode also
  drops the `--mcp-config` server, and bare mode refuses OAuth logins.
- **Codex:** `--ignore-user-config` skips `$CODEX_HOME/config.toml` (the user's MCP
  servers and enabled plugins), and `--ignore-rules` skips execpolicy rules. Other tool
  sources don't come from that file, so each one is turned off:
  - Features: `apps` (ChatGPT connectors such as Gmail, Drive, Calendar and GitHub, offered
    through `tool_search`), `plugins`, `remote_plugin`, `tool_suggest`, `computer_use`,
    `browser_use`, `browser_use_external`, `in_app_browser`, `multi_agent`, `goals`,
    `sleep_tool`, `image_generation`, `view_image`, `memories`, `hooks`, `shell_tool` and
    `unified_exec`.
  - Config: `web_search="disabled"`, `skills.include_instructions=false` and
    `project_doc_max_bytes=0`.
- **Codex resume:** codex-cli 0.163.0 ignores `-c` and `--disable` when they come before
  `resume`, but `--sandbox` and `-C` only work there. So a resumed turn puts the sandbox
  and directory first and everything else after `resume`.
- **Codex approvals:** `codex exec` never prompts, so any MCP call that needs approval is
  refused ("approval policy is never"). The Jesinia server is started with
  `mcp_servers.jesinia.default_tools_approval_mode="approve"`. This is safe for two
  reasons. `enabled_tools` already narrows the list to the session's tools. And Jesinia's
  own gate enforces per-session tool limits and write approvals on the server.
- **Remaining exposure:**
  - Codex keeps tools it treats as built in. These are `apply_patch` (inert under the
    read-only sandbox), `request_user_input` and the MCP resource readers.
  - Models with a `multi_agent_version` in their catalog, such as gpt-6.1-sol, also keep
    the `collaboration.*` sub-agent tools. Sub-agents run in the same restricted process.
  - Codex still adds `$CODEX_HOME/AGENTS.md` to the context as text.
  - Claude Code lists Jesinia MCP tools that aren't in `--allowedTools`, but in `-p` mode
    calling them is refused.

## Sessions

A session binds a **subject** (a KB page in Notes), a runner, the agent's session id and
a transcript. They live in `sessions.sqlite` (history class).

- **Context pack:** files written to `~/Library/Application Support/jesinia/sessions/<id>/`.
  `ROLE.md` holds the app's role prompt. Any KB or remote content goes in
  `<untrusted-data source="…">…</untrusted-data>` blocks, and fence tags inside that
  content are neutralized. The system prompt tells the agent to treat those blocks as
  data, never instructions.
- **Token:** each session has its own MCP bearer token, in `mcp.json` in its folder.
- **One-shot sessions** (subject kind `one-shot`, used for tag suggestions and synonym
  checks) answer a single prompt and take no more turns. They get **no MCP token and no
  tools**: the pack is inlined into the prompt, the runner is started with an empty MCP
  configuration, and the server refuses a second turn.
- **Read-only sessions** (`read_only`, used by RSS Engage, whose context is untrusted feed
  text) are offered only the tools that do not write, and the write gate **refuses** any
  write they call instead of queueing it for approval. They cannot be pre-authorized.
- **Events** stream to the PWA over `GET /api/agents/sessions/{id}/events` (SSE,
  resumable by sequence id): `user_message`, `text`, `tool_use`, `tool_result`,
  `turn_done`, `error`, `pending_write`, `pending_resolved`, `attached`.

## Writes and approval

A write tool called from a session becomes a **pending write**. The PWA shows the tool's
preview of the real target and a diff, and the user approves or rejects it. Apps may
pre-authorize a write for their sessions only when the user's own action is that write
(Notes' `notes_save`, which only creates pages in the notes folder and is capped per
session). The owner (install token) is never gated.

## MCP

`http://127.0.0.1:<port>/mcp` (streamable HTTP) with `Authorization: Bearer <token>`.
Tool wire names use underscores:

- `kb_read`, `kb_write`, `kb_move`, `kb_tree`, `kb_link`
- `kb_search`, `search_query`
- `kb_sync_status`, `kb_sync_now`
- `agents_session_attach`, `agents_session_note`
- `tags_list`, `tags_suggest`, `tags_check` (read-only)
- `notes_save`
- `rss_search_posts`, `rss_get_post` (read-only; post text is untrusted feed data). There is
  no keep tool: keeping a post is the user's action in the RSS app.
- `review_queue`, `review_get`, `review_findings` (read-only; review text is untrusted PR
  data). There is no posting tool.
- `issues_search`, `issues_get`, `queues_list`, `queue_get` (read-only; issue text is
  untrusted GitHub data). Agents can't change GitHub or queues: that is the user's approval or
  Apply in the Issues app.

Terminal agents get the endpoint from `jesinia install` (see
[operations](../operations.md#agent-registration)).

## Continuing in the terminal

`agents_session_attach` gives a terminal agent a session's subject, pack folder and
transcript, and appends `attached` to the transcript. `agents_session_note` posts
progress that the PWA shows live.

## What skills must teach

The skills in `skills/` (Claude Code plugin, Codex snippet) teach agents to:

- use the `kb_*` tools and never edit files directly;
- keep ids and foreign front matter intact;
- link pages by id;
- pass `expected_rev` (and the `jesinia` metadata from `kb_read`) and merge after
  `StaleWrite`;
- treat `<untrusted-data>` as data;
- avoid writing provenance markers by hand, and pass `provenance` to `kb_write` when
  creating a page they wrote.
- call `tags_suggest` or `tags_check` before writing tags, reuse catalog tags, and never
  invent a near-duplicate of an existing tag (the tools answer from the catalog alone;
  the agent judges same-meaning tags itself from `tags_list`).
