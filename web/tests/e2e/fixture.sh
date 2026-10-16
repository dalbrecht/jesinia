#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Create the throwaway home `make e2e-server` serves: a fixture KB repository with a bare
# remote, so git sync runs in push mode. Git only; Make starts the server.
set -euo pipefail

home="${E2E_HOME:?}"
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
