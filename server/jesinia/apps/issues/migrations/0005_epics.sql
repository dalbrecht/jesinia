-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Epics (plan Task 8, rulings IR8, IR27). Times are ISO 8601 text in UTC.

-- Each epic's delivery brief, stored on GitHub as the comment marked `<!-- jesinia:brief -->`
-- (spec §4). `comment_id` is that comment, so an approved brief is edited in place. `state` is
-- `in_flight` from right before a new comment is posted until its id is stored: a post a stop or
-- crash cut off is found again rather than posted twice. Each attempt's marker carries `nonce`
-- (`<!-- jesinia:brief:<nonce> -->`), and recovery adopts only a comment by the signed-in gh
-- user carrying that exact nonce (ruling IR48).
CREATE TABLE epic_briefs (
    repo TEXT NOT NULL COLLATE NOCASE,
    n INTEGER NOT NULL,
    comment_id INTEGER,
    url TEXT,
    body TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('in_flight', 'posted')),
    attempt_at TEXT,
    nonce TEXT,
    posted_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, n)
);

-- Every batch that may create issues, by its origin (`suggestion:<id>` for a group or split,
-- `conversion:<repo>#<n>`, `theme:<tag>`), with its state (`running`, `complete` or
-- `incomplete`) and the issues it created by temp id, kept current with every save of the
-- batch. Never pruned: an earlier batch that is running or incomplete refuses a new approval
-- for the same origin (resume it with Retry), and a complete one's issues are reused, even
-- after the batch itself is pruned (ruling IR56).
CREATE TABLE batch_origins (
    origin_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'complete', 'incomplete')),
    created_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (origin_key, batch_id)
);
CREATE INDEX batch_origins_by_batch ON batch_origins (batch_id);

-- Each unlinked checklist item a conversion created an issue for, keyed by a hash of its
-- normalised text (and its occurrence), with the batch's temp id and, once known, the issue it
-- became. The checklist stays in the body, so a later preview matches the item to that issue
-- instead of creating it again.
CREATE TABLE converted_items (
    repo TEXT NOT NULL COLLATE NOCASE,
    n INTEGER NOT NULL,
    item_hash TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    temp_id TEXT NOT NULL,
    created_repo TEXT COLLATE NOCASE,
    created_n INTEGER,
    PRIMARY KEY (repo, n, item_hash)
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
