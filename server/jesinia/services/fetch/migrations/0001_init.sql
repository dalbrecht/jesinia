-- SPDX-License-Identifier: AGPL-3.0-or-later
-- fetch.sqlite (cache): bodies kept for conditional (ETag / Last-Modified) re-fetches.
CREATE TABLE responses (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    status INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    body BLOB NOT NULL,
    fetched_at TEXT NOT NULL
);
