-- SPDX-License-Identifier: AGPL-3.0-or-later
-- When sync last read an issue's linked PRs (Codex round 3 on PR #72): a PR's own transitions
-- never edit the issue, so each sync refreshes the open issues checked longest ago (NULL: never),
-- within its linked-PR budget. ISO 8601 text in UTC.
ALTER TABLE issues ADD COLUMN linked_prs_checked_at TEXT;
