-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Agent-only locks (`jesinia: {access: agents}`, ADR 0011): one row per locked page, with
-- `scope` "folder" when the page is the folder's lock page (`kind: folder`), else "page".
CREATE TABLE page_access (
    page_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('page', 'folder'))
);

-- Existing rows predate this table: invalidate every mtime so the next scan re-reads each
-- page and records its lock.
UPDATE pages SET mtime = -1;
