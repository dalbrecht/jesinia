-- SPDX-License-Identifier: AGPL-3.0-or-later
-- rss.sqlite (history): subscriptions, polling state, posts and their read state. Read
-- state is not rebuildable, so this database is included in `jesinia backup`.
CREATE TABLE folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    position INTEGER NOT NULL
);

CREATE TABLE feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    site_url TEXT,
    folder_id INTEGER REFERENCES folders (id) ON DELETE SET NULL,
    interval_s INTEGER NOT NULL,
    next_poll_at TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_ok_at TEXT
);
CREATE INDEX feeds_next_poll ON feeds (next_poll_at);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds (id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    snippet TEXT NOT NULL,
    content_html TEXT,
    discussion_url TEXT,
    is_summary INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    full_md TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    kept_page_id TEXT,
    archive_state TEXT,
    archive_error TEXT,
    UNIQUE (feed_id, guid)
);
CREATE INDEX posts_read_published ON posts (read, published_at);
CREATE INDEX posts_url ON posts (url);

-- Idempotency keys for this module's write routes (core/idempotency.py).
CREATE TABLE idempotency (
    key TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER,
    body TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, route)
);
