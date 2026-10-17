-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Drafts and posting (spec §7, plan Task 7). A round's drafts are seeded from its findings
-- once (drafts_seeded), so a draft the author deletes stays deleted; summary is the review
-- body the author writes above any outside-the-diff drafts.
ALTER TABLE rounds ADD COLUMN drafts_seeded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rounds ADD COLUMN summary TEXT NOT NULL DEFAULT '';

-- One attempt to post a round's review, recorded before GitHub is called; its id is the
-- marker (<!-- jesinia-posting:<id> -->) ending the body it sends. A lost response, a
-- timeout or a 5xx leaves it 'uncertain' (a crash leaves 'started'): until such an attempt
-- is reconciled against the PR's reviews (adopted as 'posted', or 'not_posted'), nothing
-- else is posted for the round. 'failed' is a definite refusal. body (without the marker)
-- and comments are what was sent (spec §8: what was posted is kept with the round).
CREATE TABLE postings (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds (id) ON DELETE CASCADE,
    event TEXT NOT NULL CHECK (event IN ('APPROVE', 'REQUEST_CHANGES', 'COMMENT')),
    key TEXT NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL,
    comments TEXT NOT NULL,
    started_at TEXT NOT NULL,
    review_url TEXT,
    state TEXT NOT NULL
        CHECK (state IN ('started', 'uncertain', 'posted', 'not_posted', 'failed')),
    error TEXT,
    UNIQUE (round_id, key)
);
CREATE INDEX postings_round ON postings (round_id);
-- At most one attempt per round whose outcome isn't known.
CREATE UNIQUE INDEX postings_in_flight ON postings (round_id)
    WHERE state IN ('started', 'uncertain');
