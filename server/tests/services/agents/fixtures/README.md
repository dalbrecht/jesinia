# Agent CLI fixtures

- `claude_auth_error.jsonl`: a real `claude -p --output-format stream-json --verbose` run
  (Claude Code 2.1.301) on a machine whose CLI login had expired. Ids are scrubbed.
- `claude_turn.jsonl`, `claude_resume.jsonl`: written by hand from the documented
  stream-json schema for 2.1.301 (`system/init`, `stream_event` text deltas with
  `--include-partial-messages`, `assistant` tool_use blocks, `user` tool_result blocks,
  `result`), because a successful turn could not be recorded while the CLI was logged out.
  Re-record when a logged-in CLI is available.
- `truncated.jsonl`: `claude_turn.jsonl` cut off mid-line (stream interrupted).
- `codex_turn.jsonl`: a real `codex exec --json` turn (codex-cli 0.163.0), thread id scrubbed.
- `codex_tool.jsonl`, `codex_failed.jsonl`: the same event schema with an MCP tool call and
  with a failed turn.
