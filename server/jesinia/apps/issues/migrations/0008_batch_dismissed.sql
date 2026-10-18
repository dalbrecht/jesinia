-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A batch the author dismissed from the Pending changes tray (PWA Task 12, re-review N2): it
-- is never listed as unfinished again, unless a retry runs it (which clears the mark). ISO 8601
-- text in UTC, NULL while not dismissed.
ALTER TABLE apply_batches ADD COLUMN dismissed_at TEXT;
