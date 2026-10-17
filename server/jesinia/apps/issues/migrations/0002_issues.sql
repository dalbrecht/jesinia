-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The GitHub cache (spec §6.3 "Always cached"): watched repos with their schema and sync
-- cursor, issues with their stored triage state, labels, milestones, and the lazily fetched
-- comments, timeline and sub-issue links. Issue and comment text is untrusted and is stored
-- as data only. Times are ISO 8601 text in UTC.

-- Only repos the author added. `added_at` decides the Inbox (issues opened after it);
-- `cursor_json` is the resumable incremental-sync cursor (domain/triage.py SyncCursor).
-- `schema_json` is the effective schema and `schema_source` where it came from; the schema the
-- author made in Settings is kept apart in `settings_schema_json`, so a `.jesinia/labels.yml`
-- (which wins while it exists, spec §2) never overwrites it, and it is back when the file goes.
-- Every repo column is COLLATE NOCASE: GitHub answers any letter case, and the name GitHub
-- reports (stored at watch time) must match whatever spelling a request or payload uses.
CREATE TABLE repos (
    repo TEXT PRIMARY KEY COLLATE NOCASE,
    watched INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL,
    schema_source TEXT NOT NULL DEFAULT 'default',  -- 'default', 'repo' or 'settings'
    schema_json TEXT,
    settings_schema_json TEXT,
    migrated INTEGER NOT NULL DEFAULT 0,
    cursor_json TEXT,
    synced_at TEXT,
    sync_error TEXT
);

-- `json` is the whole issue as GitHub sent it (mapped); the other columns are what the views
-- filter and sort on, recomputed on every sync and whenever the repo's schema changes.
CREATE TABLE issues (
    repo TEXT NOT NULL COLLATE NOCASE REFERENCES repos (repo) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    json TEXT NOT NULL,
    complete INTEGER NOT NULL,
    missing_json TEXT NOT NULL,
    triage_json TEXT NOT NULL,  -- the whole Triage (readiness, priority, conflicts, ...)
    is_epic INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sub_issue_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    inbox INTEGER NOT NULL DEFAULT 0,
    sub_issues_synced_at TEXT,
    details_synced_at TEXT,
    PRIMARY KEY (repo, number)
);
CREATE INDEX issues_by_view ON issues (state, inbox, complete);

CREATE TABLE labels (
    repo TEXT NOT NULL COLLATE NOCASE REFERENCES repos (repo) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    json TEXT NOT NULL,
    PRIMARY KEY (repo, name)
);

CREATE TABLE milestones (
    repo TEXT NOT NULL COLLATE NOCASE REFERENCES repos (repo) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);

CREATE TABLE comments (
    repo TEXT NOT NULL COLLATE NOCASE,
    number INTEGER NOT NULL,
    comment_id INTEGER NOT NULL,
    json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo, number, comment_id),
    FOREIGN KEY (repo, number) REFERENCES issues (repo, number) ON DELETE CASCADE
);

CREATE TABLE timeline (
    repo TEXT NOT NULL COLLATE NOCASE,
    number INTEGER NOT NULL,
    position INTEGER NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (repo, number, position),
    FOREIGN KEY (repo, number) REFERENCES issues (repo, number) ON DELETE CASCADE
);

-- An epic's direct children in GitHub's order; a child may live in another repo of the owner.
CREATE TABLE sub_issue_links (
    repo TEXT NOT NULL COLLATE NOCASE,
    parent_number INTEGER NOT NULL,
    position INTEGER NOT NULL,
    child_repo TEXT NOT NULL COLLATE NOCASE,
    child_number INTEGER NOT NULL,
    child_id INTEGER,
    PRIMARY KEY (repo, parent_number, position),
    FOREIGN KEY (repo, parent_number) REFERENCES issues (repo, number) ON DELETE CASCADE
);

-- Previewed label setup and migration batches (domain/labels.py LabelPlan): `json` holds the
-- ops; a plan is consumed by its apply and pruned once older than PLAN_TTL.
CREATE TABLE label_plans (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL COLLATE NOCASE REFERENCES repos (repo) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
