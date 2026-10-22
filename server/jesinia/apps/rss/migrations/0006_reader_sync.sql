-- SPDX-License-Identifier: AGPL-3.0-or-later
-- NetNewsWire sync (Google Reader API) and the full-article prefetch.

-- Prefetch: when the full article was last tried for a post, and why it failed. A post
-- whose article is already cached counts as tried (when it was first seen).
ALTER TABLE posts ADD COLUMN article_attempted_at TEXT;
ALTER TABLE posts ADD COLUMN article_error TEXT;
UPDATE posts SET article_attempted_at = first_seen_at WHERE full_md IS NOT NULL;
CREATE INDEX posts_prefetch ON posts (first_seen_at)
    WHERE full_md IS NULL AND article_attempted_at IS NULL;

-- Reader API item ids are post ids, so an id is never handed out twice: without
-- AUTOINCREMENT SQLite would reuse the highest id once that post is pruned, and a client
-- would see another article under an id it already synced. New posts take ids above the
-- highest ever used.
CREATE TABLE post_id_floor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high INTEGER NOT NULL
);
INSERT INTO post_id_floor (id, high) SELECT 1, COALESCE(MAX(id), 0) FROM posts;
CREATE TRIGGER posts_raise_id_floor AFTER INSERT ON posts BEGIN
    UPDATE post_id_floor SET high = MAX(high, NEW.id) WHERE id = 1;
END;

-- Every star and unstar, append-only, so a star can always be traced to its KB page. No
-- foreign key: a row outlives the pruning of the post it names.
CREATE TABLE star_ledger (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('keep', 'unkeep')),
    source TEXT NOT NULL CHECK (source IN ('reader', 'netnewswire', 'backfill')),
    page_id TEXT,
    at TEXT NOT NULL
);
CREATE INDEX star_ledger_post ON star_ledger (post_id, id);
INSERT INTO star_ledger (post_id, action, source, page_id, at)
    SELECT id, 'keep', 'backfill', kept_page_id, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
    FROM posts WHERE kept_page_id IS NOT NULL ORDER BY id;

-- The reader password (one at a time) and the reader tokens issued under it; SHA-256 only.
CREATE TABLE reader_password (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE reader_tokens (
    sha256 TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
