-- SPDX-License-Identifier: AGPL-3.0-or-later
-- review.sqlite (history): the queue, rounds, artifacts and drafts. Rounds, drafts and posted
-- reviews can't be rebuilt from GitHub, so this database is included in `jesinia backup`.
-- Every `repo` column holds the canonical lowercase owner/name; GitHub's own spelling is
-- kept only in prs.display_repo, for the UI.

CREATE TABLE repos (
    repo TEXT PRIMARY KEY,
    watched INTEGER NOT NULL DEFAULT 0,
    automated_reviewer TEXT,
    prereview TEXT CHECK (prereview IN ('triage', 'everything', 'on_open'))
);

-- One row per PR in the queue. Beyond the planned columns: display_repo, url, base_sha
-- (kept by the prune), review_requested (queue order), the bot/human review counts, the
-- state's reason, and fetch_error/fetch_error_at, set when GitHub couldn't load the PR on
-- the last sync (the cached fields are then left as they were).
CREATE TABLE prs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    display_repo TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    author_is_bot INTEGER NOT NULL,
    url TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    head_ref TEXT NOT NULL,
    head_repo TEXT,
    created_at TEXT NOT NULL,
    size_lines INTEGER NOT NULL,
    files INTEGER NOT NULL,
    review_requested INTEGER NOT NULL,
    ci_state TEXT NOT NULL,
    auto_review_state TEXT NOT NULL,
    review_comment_count INTEGER NOT NULL,
    bot_review_count INTEGER NOT NULL,
    human_review_count INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'new',
    depth TEXT NOT NULL CHECK (depth IN ('quick', 'deep')),
    state TEXT NOT NULL DEFAULT 'debouncing',
    state_reason TEXT NOT NULL DEFAULT '',
    last_head_change_at TEXT NOT NULL,
    loaded INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    fetch_error TEXT,
    fetch_error_at TEXT,
    PRIMARY KEY (repo, number)
);

-- One review pass over one head. Beyond the plan: `error` keeps why a pre-review failed, and
-- `interrupted` marks one a stop or restart cut short (re-queued, up to 3 times per head).
CREATE TABLE rounds (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    depth TEXT NOT NULL CHECK (depth IN ('quick', 'deep')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    verdict TEXT,
    posted_review_url TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX rounds_pr_head ON rounds (repo, number, head_sha);

CREATE TABLE artifacts (
    round_id INTEGER NOT NULL REFERENCES rounds (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (round_id, kind)
);

-- line/side are NULL for outside-the-diff drafts, which post in the review body.
CREATE TABLE drafts (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    line INTEGER,
    side TEXT CHECK (side IN ('LEFT', 'RIGHT')),
    start_line INTEGER,
    start_side TEXT CHECK (start_side IN ('LEFT', 'RIGHT')),
    body TEXT NOT NULL,
    origin_finding TEXT,
    outdated INTEGER NOT NULL DEFAULT 0,
    outside_diff INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX drafts_round ON drafts (round_id);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    json TEXT NOT NULL
);

-- PR files (with their patches) per head, so an opened PR renders from the cache.
CREATE TABLE pr_files (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (repo, number, head_sha)
);

-- Idempotency keys for this module's write routes (core/idempotency.py).
CREATE TABLE idempotency (
    key TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER,
    body TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, route)
);
