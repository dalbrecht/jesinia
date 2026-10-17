-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Review standards set in the app (spec §5.2, Settings → Review): the author's own rules
-- (repo NULL, for every repo) and per-repo ones (canonical owner/name), applied on top of
-- the base and repo layers. heading_path is a JSON list of headings under doc.
CREATE TABLE standards_overrides (
    id INTEGER PRIMARY KEY,
    repo TEXT,
    doc TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX standards_overrides_repo ON standards_overrides (repo);
