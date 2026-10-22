-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A feed remembers the date of its newest post (pruning must not make a feed look dormant)
-- and may override the global retention with its own window in days or cap in posts.
ALTER TABLE feeds ADD COLUMN newest_post_at TEXT;
ALTER TABLE feeds ADD COLUMN retention_unit TEXT CHECK (retention_unit IN ('days', 'posts'));
ALTER TABLE feeds ADD COLUMN retention_value INTEGER;
UPDATE feeds SET newest_post_at = (
    SELECT MAX(COALESCE(published_at, first_seen_at)) FROM posts WHERE feed_id = feeds.id
);
