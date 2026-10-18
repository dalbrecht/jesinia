-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Themes (plan Task 10, rulings IR15, IR22, IR47). Local only: none of this is on GitHub, so
-- it can't be rebuilt from a sync (`issues.sqlite` is a history database). Times are ISO 8601
-- text in UTC.

-- Each issue's themes: tags from the KB-wide catalog, in the catalog's canonical form (already
-- case-folded, so compared as stored). Removing a theme deletes its row only; the catalog
-- entry stays (IR22).
CREATE TABLE issue_themes (
    repo TEXT NOT NULL COLLATE NOCASE,
    n INTEGER NOT NULL,
    tag TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (repo, n, tag)
);
-- A theme's issues, for its drop target, its filter, publishing and its epic.
CREATE INDEX issue_themes_by_tag ON issue_themes (tag);

-- The batches that turned a theme into an epic: while one runs, or one created the epic and
-- didn't finish, approving again is refused (it would create a second epic).
CREATE TABLE theme_epics (
    tag TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tag, batch_id)
);
