-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Work queues (plan Task 9, rulings IR15, IR47). Local only: none of this is on GitHub, so it
-- can't be rebuilt from a sync (`issues.sqlite` is a history database). Times are ISO 8601 text
-- in UTC.

-- A named queue with an optional description, in the author's order of queues. Names compare
-- without case.
CREATE TABLE queues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Each queue's items in order. `ref` is `owner/repo#n` (for a milestone, its number). An issue
-- and the same issue as an epic are one item (the application keeps one row per issue).
-- `position` orders open and done items alike, so a done item keeps its place for Undo.
-- `done_pending` is the pending change Done staged (NULL when the issue closed on GitHub).
CREATE TABLE queue_items (
    queue_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issue', 'epic', 'milestone')),
    ref TEXT NOT NULL COLLATE NOCASE,
    position INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    done_at TEXT,
    done_pending TEXT,
    PRIMARY KEY (queue_id, kind, ref)
);
-- Sync's closed-issue hook finds every queue's open items for an issue.
CREATE INDEX queue_items_by_ref ON queue_items (ref, kind);
