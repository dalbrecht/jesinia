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
# A kept post: Notes opens it in the Read view, with its archived image (local-only, like
# the RSS archive: the asset folder's .gitignore keeps it out of git).
kept=0199e000-0000-7000-8000-000000000002
mkdir -p "$home/kb/Sources" "$home/kb/_assets/$kept"
cat > "$home/kb/Sources/Kept fixture.md" <<PAGE
---
id: $kept
title: Kept fixture
jesinia:
  kind: source/rss
  source: https://blog.example/kept
---
A note on [[0199e000-0000-7000-8000-000000000001|a sibling page]].

<!-- jesinia:ai agent=claude-code model=m at=2026-10-01T00:00:00Z session=s1 -->
Takeaway: a kept post renders.
<!-- /jesinia:ai -->

## Article

![A red square](../_assets/$kept/red.png)

Remote: ![a remote chart](https://img.example/c.png)
PAGE
printf '*\n' > "$home/kb/_assets/$kept/.gitignore"
# A 16x16 red PNG.
printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR42mM4YGBAEmIY1TCqYfhqAACs5yAQdN/vpQAAAABJRU5ErkJggg==' \
  | base64 -d > "$home/kb/_assets/$kept/red.png"
git -C "$home/kb" add -A
git -C "$home/kb" commit -qm "fixture"
git -C "$home/kb" remote add origin "$home/remote.git"
git -C "$home/kb" push -q -u origin main

# The fake GitHub's repository (web/tests/e2e/fixtures/github/server.py serves its API; the
# review workspace fetches these commits from this bare repo instead of github.com). Two PR
# branches off main: a small fix with a test (#1, CI passing) and a feature still on CI (#2).
gh_remote="$home/github-repos/e2e-dev/greeter.git"
gh_work="$home/github-work"
mkdir -p "$(dirname "$gh_remote")"
git init -q --bare -b main "$gh_remote"
git init -q -b main "$gh_work"
mkdir -p "$gh_work/src"
cat > "$gh_work/src/greeter.py" <<'PY'
"""Greetings."""


def greet(name):
    return "Hello, " + name


def farewell(name):
    return "Bye, " + name
PY
git -C "$gh_work" add -A
git -C "$gh_work" commit -qm "Add greeter"
git -C "$gh_work" checkout -q -b greeter-empty-name
cat > "$gh_work/src/greeter.py" <<'PY'
"""Greetings."""


def greet(name):
    if not name:
        return "Hello, stranger"
    return "Hello, " + name


def farewell(name):
    return "Bye, " + name
PY
mkdir -p "$gh_work/tests"
cat > "$gh_work/tests/test_greeter.py" <<'PY'
from src.greeter import greet


def test_greet_handles_an_empty_name():
    assert greet("") == "Hello, stranger"
PY
git -C "$gh_work" add -A
git -C "$gh_work" commit -qm "Handle empty names in greet"
git -C "$gh_work" checkout -q main
git -C "$gh_work" checkout -q -b farewell-shout
sed -i.bak 's/return "Bye, " + name/return "BYE, " + name.upper()/' "$gh_work/src/greeter.py"
rm "$gh_work/src/greeter.py.bak"
git -C "$gh_work" commit -qam "Shout farewells"
git -C "$gh_work" push -q "$gh_remote" main greeter-empty-name farewell-shout
rm -rf "$gh_work"
