# Agents

How Jesinia works with Claude Code and Codex (ADR 0005).

## Runners

| Runner | Command (abridged) | Confinement |
|---|---|---|
| `claude-code` | `claude -p --output-format stream-json --verbose --include-partial-messages --strict-mcp-config --mcp-config <session>/mcp.json` | Allowed tools: `Read` inside the session folder plus `mcp__jesinia__*`. Bash, Edit, Write, WebFetch and similar are disallowed |
| `codex` | `codex exec --json --sandbox read-only --disable shell_tool -C <session> -c mcp_servers.jesinia.…` | Read-only sandbox, shell off, MCP tools allowlisted |

- Both use the CLI's own login, a minimal environment, and the prompt on stdin. A
  follow-up turn resumes the agent's session id.
- `JESINIA_CLAUDE_MODEL` and `JESINIA_CODEX_MODEL` pick a model.
- Availability (`/api/agents/runners`) shows in Settings and in `jesinia doctor`.

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
