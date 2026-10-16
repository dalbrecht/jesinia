#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Start a throwaway Jesinia server for Playwright: a temp JESINIA_HOME and a fixture KB
# repository with a bare remote, so git sync runs in push mode.
set -euo pipefail

home="${E2E_HOME:?}"
port="${E2E_PORT:?}"
server="$(cd "$(dirname "$0")/../../../server" && pwd)"
rm -rf "$home"
mkdir -p "$home"

export GIT_AUTHOR_NAME="Jesinia E2E" GIT_AUTHOR_EMAIL="e2e@example.invalid"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
git init -q --bare -b main "$home/remote.git"
git init -q -b main "$home/kb"
mkdir -p "$home/kb/Notes"
printf 'A plain note, adopted by Jesinia on first scan.\n' > "$home/kb/Notes/hello.md"
git -C "$home/kb" add -A
git -C "$home/kb" commit -qm "fixture"
git -C "$home/kb" remote add origin "$home/remote.git"
git -C "$home/kb" push -q -u origin main

export JESINIA_HOME="$home/data"
uv run --project "$server" jesinia config set server.port "$port" >/dev/null
uv run --project "$server" jesinia config set kbsync.quiet 1s >/dev/null
exec uv run --project "$server" jesinia serve
