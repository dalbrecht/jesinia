-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Settings → Issues' own settings (PWA Task 14), one row: the agent runner that answers
-- suggestion asks, or NULL for the agents service's preferred one.
CREATE TABLE settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    runner TEXT
);
INSERT INTO settings (id, runner) VALUES (1, NULL);
