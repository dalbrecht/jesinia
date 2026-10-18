-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Stack reviews (spec §4.3; plan Task 13). A stack review is the stack's one Overview,
-- Background and stack walkthrough, for one set of PRs at one set of heads: `signature` is
-- "<n>@<head>,..." bottom-up, so any push to any PR makes a new stack review (the old one
-- stays readable). Each PR keeps its own round, drafts and postings.
CREATE TABLE stack_reviews (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    signature TEXT NOT NULL,
    started_at TEXT NOT NULL,
    UNIQUE (repo, signature)
);

-- A stack review's artifacts by kind (agent output, schema-checked; and `jobs`, each stack
-- job's state and cause), as `artifacts` holds a round's.
CREATE TABLE stack_artifacts (
    stack_id INTEGER NOT NULL REFERENCES stack_reviews (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (stack_id, kind)
);
