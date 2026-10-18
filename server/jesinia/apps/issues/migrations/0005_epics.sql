-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Epics (plan Task 8, rulings IR8, IR27). Times are ISO 8601 text in UTC.

-- Each epic's delivery brief, stored on GitHub as the comment marked `<!-- jesinia:brief -->`
-- (spec §4). `comment_id` is that comment, so an approved brief is edited in place. `state` is
-- `in_flight` from right before a new comment is posted until its id is stored: a post a stop or
-- crash cut off is found again by its marker (from `attempt_at` on) rather than posted twice.
CREATE TABLE epic_briefs (
    repo TEXT NOT NULL COLLATE NOCASE,
    n INTEGER NOT NULL,
    comment_id INTEGER,
    url TEXT,
    body TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('in_flight', 'posted')),
    attempt_at TEXT,
    posted_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, n)
);

-- A checklist conversion is a batch of its own (`epic`), beside the tray's and approvals'.
-- SQLite can't alter a CHECK, so the table is rebuilt with its rows and indexes.
CREATE TABLE apply_batches_next (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('pending', 'suggestion', 'epic')),
    suggestion_id TEXT,
    status TEXT NOT NULL DEFAULT 'finished' CHECK (status IN ('running', 'finished', 'interrupted')),
    request_key TEXT,
    batch_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO apply_batches_next (
    id, origin, suggestion_id, status, request_key, batch_json, result_json, created_at, updated_at
)
SELECT id, origin, suggestion_id, status, request_key, batch_json, result_json, created_at,
    updated_at
FROM apply_batches;
DROP TABLE apply_batches;
ALTER TABLE apply_batches_next RENAME TO apply_batches;
CREATE INDEX apply_batches_by_age ON apply_batches (updated_at);
CREATE UNIQUE INDEX apply_batches_by_key ON apply_batches (request_key)
    WHERE request_key IS NOT NULL;
