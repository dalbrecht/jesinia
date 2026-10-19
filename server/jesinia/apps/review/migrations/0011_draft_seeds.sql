-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Drafts are seeded per finding kind (the correctness pass, the findings job): each kind's
-- findings become drafts once, when its artifact first exists, so a failed job (or a Retry
-- that reruns it in the same round) never uses up the seeding of findings that land later,
-- and a draft the author deletes after its kind was seeded stays deleted.
-- rounds.drafts_seeded is no longer read.
CREATE TABLE draft_seeds (
    round_id INTEGER NOT NULL REFERENCES rounds (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('correctness', 'findings')),
    PRIMARY KEY (round_id, kind)
);
-- How many finding drafts were left out because the round had no room under the draft limit
-- (500): by seeding, or by the author's save that first took them. They are gone for good,
-- so every read of the drafts reports the count.
ALTER TABLE rounds ADD COLUMN seeds_dropped INTEGER NOT NULL DEFAULT 0;

-- A round seeded before this counts as seeded for the kinds whose artifact it has, but only
-- if it holds a finding draft: a seeded round with none was (almost always) claimed before
-- any finding had arrived, by a job that failed, so it is seeded again from what is there
-- now. Kinds can't be told apart from a draft (origin_finding is group/severity), and the
-- only cost of this rule is a finding the author deleted from a round left with no finding
-- draft at all coming back once (a carried finding draft counts as one, so such a round
-- stays seeded).
INSERT INTO draft_seeds (round_id, kind)
SELECT a.round_id, a.kind
FROM artifacts AS a
JOIN rounds AS r ON r.id = a.round_id
WHERE r.drafts_seeded = 1
    AND a.kind IN ('correctness', 'findings')
    AND EXISTS (
        SELECT 1 FROM drafts AS d WHERE d.round_id = r.id AND d.origin_finding IS NOT NULL
    );
