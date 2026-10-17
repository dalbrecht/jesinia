-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The apply engine (plan Task 6, rulings IR14, IR16, IR18). Times are ISO 8601 text in UTC.

-- Why a suggestion's last approval failed to apply; it stays approvable.
ALTER TABLE suggestions ADD COLUMN apply_error TEXT;
-- Set while an approval of the suggestion is applying (IR40): retiring and reruns leave a
-- claimed draft alone, so the approval's outcome always has its row. Cleared at start.
ALTER TABLE suggestions ADD COLUMN claimed_at TEXT;

-- Every applied batch (a pending-tray apply or a suggestion's approval) with its result, step by
-- step, so Retry re-runs only the steps that didn't apply (IR14). `batch_json` is the changes
-- (domain/changes.py batch_json); `result_json` lines up with their series. `suggestion_id` is
-- the approved suggestion, marked approved once its batch completes (on a retry too).
CREATE TABLE apply_batches (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('pending', 'suggestion')),
    suggestion_id TEXT,
    batch_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX apply_batches_by_age ON apply_batches (updated_at);

-- The tray's rows are applied in the order they were staged.
CREATE INDEX pending_by_age ON pending (created_at);
