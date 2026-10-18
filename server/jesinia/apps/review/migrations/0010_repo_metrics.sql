-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A repo's own metric set (spec §5.2, Settings → Review): a JSON list of criteria its reviews
-- are scored on, or NULL when the standards decide. Each round keeps the set in force when it
-- started (its metric_criteria artifact, taken by start_round), so a change applies from the
-- next round. Rounds from before this migration are scored on the standards' criteria.
ALTER TABLE repos ADD COLUMN metrics TEXT;
INSERT OR IGNORE INTO artifacts (round_id, kind, json)
SELECT id, 'metric_criteria', 'null' FROM rounds;
