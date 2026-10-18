-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The depth the author chose for a PR's head (spec §3.1, ruling R57): it wins over Jesinia's
-- proposal whenever a round on that head is prepared, across restarts.
CREATE TABLE depth_choices (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    depth TEXT NOT NULL,
    PRIMARY KEY (repo, number, head_sha)
);
