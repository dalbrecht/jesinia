#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Manual smoke test (real agents, real API usage): start a throwaway Jesinia server
# with a fixture KB, ask Claude Code and Codex to save a note through MCP, and check
# that each note landed with provenance. A CLI that is not installed is skipped.
#
#   skills/smoke.sh
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
home="$(mktemp -d)"
port=4879
trap 'kill "${server_pid:-}" 2>/dev/null || true; rm -rf "$home"' EXIT

mkdir -p "$home/kb/Ideas"
printf 'Smoke topic: why small notes compound.\n' > "$home/kb/Ideas/Smoke topic.md"
export JESINIA_HOME="$home/data"
jesinia() { uv run --project "$repo/server" jesinia "$@"; }
jesinia config set server.port "$port" >/dev/null
jesinia config set kb.path "$home/kb" >/dev/null
token="$(jesinia token show)"
jesinia serve >"$home/server.log" 2>&1 &
server_pid=$!
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 0.5; done

prompt='Use the jesinia MCP tools. Find the page "Smoke topic" with kb_search, read it with kb_read, then create a page with kb_write: title "Smoke note from AGENT", folder "Notes", a two-sentence body about it that ends with a [[id|Smoke topic]] link. Reply with the saved path.'
status=0

if command -v claude >/dev/null; then
  mcp="$home/claude-mcp.json"
  printf '{"mcpServers":{"jesinia":{"type":"http","url":"http://127.0.0.1:%s/mcp","headers":{"Authorization":"Bearer %s"}}}}' "$port" "$token" >"$mcp"
  claude -p "${prompt//AGENT/claude}" --mcp-config "$mcp" --strict-mcp-config \
    --allowedTools "mcp__jesinia__kb_search,mcp__jesinia__kb_read,mcp__jesinia__kb_write" >/dev/null || true
  if [[ -f "$home/kb/Notes/Smoke note from claude.md" ]]; then echo "ok   claude"; else echo "FAIL claude: no note"; status=1; fi
else
  echo "skip claude (not installed)"
fi

if command -v codex >/dev/null; then
  JESINIA_TOKEN="$token" codex exec --skip-git-repo-check --sandbox read-only \
    -c "mcp_servers.jesinia.url=\"http://127.0.0.1:$port/mcp\"" \
    -c 'mcp_servers.jesinia.bearer_token_env_var="JESINIA_TOKEN"' \
    "${prompt//AGENT/codex}" >/dev/null || true
  if [[ -f "$home/kb/Notes/Smoke note from codex.md" ]]; then echo "ok   codex"; else echo "FAIL codex: no note"; status=1; fi
else
  echo "skip codex (not installed)"
fi

exit "$status"
