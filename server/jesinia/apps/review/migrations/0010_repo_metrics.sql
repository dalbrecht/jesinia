-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A repo's own metric set (spec §5.2, Settings → Review): a JSON list of criteria its reviews
-- are scored on, or NULL when the standards decide. Each round keeps the set it was prepared
-- with (its metric_criteria artifact), so a change applies from the next round.
ALTER TABLE repos ADD COLUMN metrics TEXT;
