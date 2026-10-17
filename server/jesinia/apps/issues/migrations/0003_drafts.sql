-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Drafts (plan Task 5, rulings IR11, IR35): agent suggestions, the pending-changes tray and
-- the approved schema overlay. All of it is the author's own work, which GitHub can't give
-- back. Times are ISO 8601 text in UTC.

-- Suggestions are drafts until the author approves or dismisses them (domain/suggestions.py).
-- One about a single issue has `repo` and `n`; one about several issues or a queue (later
-- tasks) has `scope` instead. `payload_json` is Jesinia's own JSON, shaped per kind.
-- `origin_json` is the advice it was proposed as (kept through edits); `basis` is the issue's
-- content hash when it was dismissed (a dismissal holds only while that is unchanged, IR38a).
CREATE TABLE suggestions (
    id TEXT PRIMARY KEY,
    repo TEXT COLLATE NOCASE,
    n INTEGER,
    scope TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'label', 'duplicate', 'parent', 'readiness_gap', 'new_area', 'priority', 'group',
        'split', 'queue_next', 'queue_order', 'queue_split', 'brief'
    )),
    payload_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'edited', 'approved', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    origin_json TEXT,
    basis TEXT,
    CHECK ((repo IS NULL) = (n IS NULL)),
    CHECK (repo IS NOT NULL OR scope IS NOT NULL)
);
CREATE INDEX suggestions_by_issue ON suggestions (repo, n, state);

-- Each issue's latest suggestion run: queued (due at `due_at`), running, done, skipped (it
-- left the Inbox first), waiting (no runner) or failed (`cause`). `source` is `inbox` for a
-- background run, `demand` for one the author asked for.
CREATE TABLE suggestion_runs (
    repo TEXT NOT NULL COLLATE NOCASE,
    n INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'done', 'skipped', 'waiting', 'failed'
    )),
    source TEXT NOT NULL CHECK (source IN ('inbox', 'demand')),
    due_at TEXT NOT NULL,
    cause TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, n)
);
CREATE INDEX suggestion_runs_due ON suggestion_runs (status, due_at);

-- The pending-changes tray (Task 6): GitHub changes staged by drags, pickers, menus and the
-- bulk bar until Apply.
CREATE TABLE pending (
    id TEXT PRIMARY KEY,
    ref TEXT NOT NULL,
    change_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL
);

-- Schema entries the author approved (a `new_area`, a published `theme:`), merged over the
-- repo's schema on every read, even a read-only labels.yml (Task 6).
CREATE TABLE schema_overlay (
    repo TEXT NOT NULL COLLATE NOCASE,
    dimension TEXT NOT NULL,
    canonical TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    approved_at TEXT NOT NULL,
    PRIMARY KEY (repo, dimension, canonical)
);
