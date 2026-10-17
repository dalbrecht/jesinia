-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Reading: the newest-first list order, and the GUIDs of pruned posts, so a feed that still
-- offers an old item cannot bring it back as new. A tombstone is forgotten once the feed
-- has stopped offering the item for a while (seen_at is refreshed on every poll that does).
CREATE INDEX posts_sort ON posts (COALESCE(published_at, first_seen_at), id);

CREATE TABLE pruned_posts (
    feed_id INTEGER NOT NULL REFERENCES feeds (id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (feed_id, guid)
);
