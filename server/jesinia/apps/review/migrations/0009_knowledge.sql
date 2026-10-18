-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The knowledge model (spec §6.1, plan Task 14): private derived data about what the author
-- knows. A unit is `repo/<owner>/<name>[/<path>]` (a folder at most 3 deep, or the root) or
-- `tag/<tag>`. Times are ISO 8601 UTC text with microseconds, so they sort as text.

-- Each signal is stored once, keyed by its source (`commit:<repo>:<sha>`, `round:<id>`,
-- `quiz:<id>`), even when it exposed no unit. KB pages are left out for now (R84): their
-- authorship is unknown, so they aren't evidence of the author's knowledge.
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
        kind IN ('commit', 'review_quick', 'review_deep', 'quiz_pass', 'quiz_fail')
    ),
    at TEXT NOT NULL,
    repo TEXT,
    path TEXT,
    -- 1 when the source touched the unit's own files (it is a file's deepest unit); 0 when
    -- it reached the unit only through a folder below it.
    direct INTEGER NOT NULL DEFAULT 1,
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

-- The commits the author reviewed (R87): each posted or done round's range (merge base to
-- head) and its PR's merge commit once it landed. Only these are exempt from churn: an
-- author email is anyone's to write.
CREATE TABLE knowledge_reviewed_commits (
    repo TEXT NOT NULL,
    sha TEXT NOT NULL,
    PRIMARY KEY (repo, sha)
);

-- Rounds whose reviewed commits are listed; `merge_settled` once the PR landed or closed.
CREATE TABLE knowledge_reviews (
    round_id INTEGER PRIMARY KEY REFERENCES rounds (id) ON DELETE CASCADE,
    listed_at TEXT NOT NULL,
    merge_settled INTEGER NOT NULL DEFAULT 0
);

-- How far each repo's history was read: its default-branch tip, and a digest of the
-- author's emails then (other emails read the history again).
CREATE TABLE knowledge_repos (
    repo TEXT PRIMARY KEY,
    tip TEXT NOT NULL,
    emails TEXT NOT NULL,
    read_at TEXT NOT NULL
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
    -- The quiz's question count; a skipped question counts as not right.
    questions INTEGER,
    recorded_at TEXT NOT NULL
);
