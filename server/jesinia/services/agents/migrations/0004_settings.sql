-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Settings → Agents (each runner's model, key "model.<runner>"), and idempotency keys for this
-- module's write routes (core/idempotency.py).
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    json TEXT NOT NULL
);

CREATE TABLE idempotency (
    key TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER,
    body TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, route)
);
