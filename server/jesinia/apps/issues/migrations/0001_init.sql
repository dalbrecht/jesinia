-- SPDX-License-Identifier: AGPL-3.0-or-later
-- issues.sqlite (history): user-authored queues, order, notes, themes and drafts that can't
-- be rebuilt from GitHub, so this database is included in `jesinia backup`. The issue cache
-- and the rest arrive in later migrations (0002 onwards).

-- Idempotency keys for this module's write routes (core/idempotency.py).
CREATE TABLE idempotency (
    key TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER,
    body TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, route)
);
