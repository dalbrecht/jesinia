-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Rebases and head changes (spec §4.1, §7; plan Task 11).
--
-- A round a newer round replaced is frozen: read-only, its drafts moved to `successor_id`.
ALTER TABLE rounds ADD COLUMN frozen_at TEXT;
ALTER TABLE rounds ADD COLUMN successor_id INTEGER REFERENCES rounds (id);
-- A draft carried over from an earlier head keeps that head (and its base) until it is
-- re-anchored to the new head's lines, or stays outdated: NULL once its lines are this
-- round's.
ALTER TABLE drafts ADD COLUMN carried_head TEXT;
ALTER TABLE drafts ADD COLUMN carried_base TEXT;

-- What each PR head needs after the author's previous review (spec §4.1): still approved
-- (a rebase with the same per-file patch IDs), a re-review of only the files that differ,
-- or a re-review, by content only: CI is applied each time it is read. One row per current
-- head (a sync drops the rest); differing_paths is a JSON list. `error`: the head couldn't be
-- checked (GitHub or git failed); it is checked again on the next sync. prev_merge_base and
-- prev_round: the previous review's merge base and stored round, which a re-review builds
-- on (spec §4.2; Task 12).
CREATE TABLE followups (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('new', 'still_approved', 'rereview', 'changed_hunks_only')),
    differing_paths TEXT NOT NULL,
    note TEXT NOT NULL,
    prev_head TEXT,
    prev_source TEXT CHECK (prev_source IN ('round', 'github')),
    error TEXT,
    prev_merge_base TEXT,
    prev_round INTEGER,
    PRIMARY KEY (repo, number, head_sha)
);
