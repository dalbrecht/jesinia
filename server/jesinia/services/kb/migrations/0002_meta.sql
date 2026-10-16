-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Which KB root the index describes, so a root changed while the server was stopped
-- is detected at startup (the old pages are not "missing").
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
