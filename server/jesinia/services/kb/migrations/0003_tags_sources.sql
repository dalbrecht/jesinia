-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Tags (from page bodies) and source URLs (from `jesinia.source`), for the KB-wide tag catalog.
CREATE TABLE page_tags (
    page_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (page_id, tag)
);
CREATE INDEX page_tags_by_tag ON page_tags (tag);

CREATE TABLE page_sources (
    page_id TEXT PRIMARY KEY,
    url TEXT NOT NULL
);
CREATE INDEX page_sources_by_url ON page_sources (url);

-- Existing rows predate these tables. A scan skips files whose mtime matches the index,
-- so invalidate every mtime and the next scan re-reads each page and fills the new tables.
UPDATE pages SET mtime = -1;
