-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The knowledge model (spec §6.1, plan Task 14): private derived data about what the author
-- knows. A unit is `repo/<owner>/<name>[/<path>]` (a folder at most 3 deep, or the root) or
-- `tag/<tag>`. Times are ISO 8601 UTC text with microseconds, so they sort as text.

-- Each signal is stored once, keyed by its source (`commit:<repo>:<sha>`, `round:<id>`,
-- `quiz:<id>`, `kb:<page>@<rev>`), even when it exposed no unit.
CREATE TABLE knowledge_sources (
    source TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL
);

-- What each source exposed: one row per unit. `repo`/`path` are set for path units, so
-- changes since the last exposure can be joined against `knowledge_churn`.
CREATE TABLE knowledge_exposures (
    source TEXT NOT NULL REFERENCES knowledge_sources (source) ON DELETE CASCADE,
    unit TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('commit', 'review_quick', 'review_deep', 'quiz_pass', 'quiz_fail', 'kb_page')
    ),
    at TEXT NOT NULL,
    repo TEXT,
    path TEXT,
    PRIMARY KEY (source, unit)
);
CREATE INDEX knowledge_exposures_unit ON knowledge_exposures (unit);

-- Lines each default-branch commit changed per unit path (anyone's commits), for
-- "how much the code has changed since" the author's last exposure.
CREATE TABLE knowledge_churn (
    repo TEXT NOT NULL,
    sha TEXT NOT NULL,
    path TEXT NOT NULL,
    at TEXT NOT NULL,
    lines INTEGER NOT NULL CHECK (lines >= 0),
    PRIMARY KEY (repo, sha, path)
);
CREATE INDEX knowledge_churn_unit ON knowledge_churn (repo, path, at);

-- How far each repo's history was read: its default-branch tip, and a digest of the
-- author's emails then (other emails read the history again).
CREATE TABLE knowledge_repos (
    repo TEXT PRIMARY KEY,
    tip TEXT NOT NULL,
    emails TEXT NOT NULL,
    read_at TEXT NOT NULL
);

-- The revision each KB page was last read at, so an unchanged page is never read again.
CREATE TABLE knowledge_pages (
    page_id TEXT PRIMARY KEY,
    rev TEXT NOT NULL
);

-- The author's corrections (Settings → Review, "What Jesinia thinks you know").
CREATE TABLE knowledge_overrides (
    unit TEXT PRIMARY KEY,
    override TEXT NOT NULL CHECK (override IN ('known', 'unknown')),
    set_at TEXT NOT NULL
);

-- A round's quiz result (spec §3.1: recorded, feeds the model); the first result counts.
CREATE TABLE quiz_results (
    round_id INTEGER PRIMARY KEY REFERENCES rounds (id) ON DELETE CASCADE,
    correct INTEGER NOT NULL CHECK (correct >= 0),
    answered INTEGER NOT NULL CHECK (answered > 0 AND correct <= answered),
    recorded_at TEXT NOT NULL
);
