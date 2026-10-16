-- SPDX-License-Identifier: AGPL-3.0-or-later
-- github.sqlite (cache): ETag-validated REST responses; rows older than 30 days are pruned.
CREATE TABLE responses (
    key TEXT PRIMARY KEY,
    etag TEXT NOT NULL,
    status INTEGER NOT NULL,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
