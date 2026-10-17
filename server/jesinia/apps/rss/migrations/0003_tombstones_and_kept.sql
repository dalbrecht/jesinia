-- SPDX-License-Identifier: AGPL-3.0-or-later
-- When each feed was last read in full (parsed). A pruned post's tombstone expires only once
-- the feed has been re-read for TOMBSTONE_TTL without offering it; a feed that answers
-- "unchanged" is not re-read, so its tombstones never age out from under it. Unknown for
-- existing feeds until their next full read (their tombstones wait until then).
ALTER TABLE feeds ADD COLUMN last_parsed_at TEXT;

-- Keep and Retry look posts up by their kept page; the kept view lists them.
CREATE INDEX posts_kept ON posts (kept_page_id) WHERE kept_page_id IS NOT NULL;
