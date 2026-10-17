-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A read-only session (e.g. RSS Engage over untrusted feed text) is offered no write tools,
-- and the write gate refuses any it calls instead of asking the user to approve them.
ALTER TABLE sessions ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0;
