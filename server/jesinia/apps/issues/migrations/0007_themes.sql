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

-- The batches that turned a theme into an epic, and the epic each made or linked under
-- (`epic_repo`, `epic_n`, once known). Approving again is refused while one is running or
-- unfinished (`batch_origins`, IR56); the theme's epic is reused for issues added later, even
-- after its batch is pruned.
CREATE TABLE theme_epics (
    tag TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    epic_repo TEXT COLLATE NOCASE,
    epic_n INTEGER,
    PRIMARY KEY (tag, batch_id)
);
