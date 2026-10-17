-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Unsubscribing hides a feed instead of deleting it: its kept posts (and their archives,
-- which record their outcome on the post) outlive it in the Kept view. Its other posts are
-- deleted at once; subscribing to the same URL again restores the feed.
ALTER TABLE feeds ADD COLUMN deleted_at TEXT;
