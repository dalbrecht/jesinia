-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Idempotency keys for the kb write routes (jesinia.core.idempotency); pruned after 7 days.
CREATE TABLE idempotency (
    key TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER,
    body TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, route)
);
CREATE INDEX idempotency_by_created_at ON idempotency (created_at);
