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
mkdir -p "$home/kb/Ideas"
printf 'A plain note, adopted by Jesinia on first scan.\n' > "$home/kb/Notes/hello.md"
printf 'The fixture topic: how small notes grow into a knowledge base.\n' > "$home/kb/Ideas/Fixture topic.md"
# Noncanonical front matter (quoted title, flow list, a YAML comment) and a provenance
# block: editing the body must leave both byte-identical.
cat > "$home/kb/Ideas/Round Trip.md" <<'PAGE'
---
title: "Round Trip"
id: 0199e000-0000-7000-8000-000000000001
aliases: [rt]
# a YAML comment
---
Intro paragraph.

<!-- jesinia:ai agent=claude-code model=m at=2026-10-01T00:00:00Z session=s1 -->
Earlier AI text.
<!-- /jesinia:ai -->
PAGE
git -C "$home/kb" add -A
git -C "$home/kb" commit -qm "fixture"
git -C "$home/kb" remote add origin "$home/remote.git"
git -C "$home/kb" push -q -u origin main
