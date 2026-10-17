-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The author's **Review anyway** per PR head (ruling R66): only this lets later opens, Retry,
-- Walk everything and a depth switch on that head past the CI and automated-review gates.
-- A new head has no row, so its gate holds again.
CREATE TABLE gate_overrides (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    PRIMARY KEY (repo, number, head_sha)
);
