-- SPDX-License-Identifier: AGPL-3.0-or-later
-- sessions.sqlite (history): agent sessions and transcripts. Not reconstructible; backed up.
CREATE TABLE sessions (
    id               TEXT PRIMARY KEY,
    subject_kind     TEXT NOT NULL,
    subject_ref      TEXT NOT NULL,
    subject_title    TEXT NOT NULL,
    runner           TEXT NOT NULL,
    agent_session_id TEXT,
    model            TEXT,
    status           TEXT NOT NULL,       -- idle | running | failed
    preauthorized    TEXT NOT NULL,       -- JSON list of tool names
    created_at       TEXT NOT NULL
);
CREATE TABLE events (
    session_id TEXT NOT NULL REFERENCES sessions (id),
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    data       TEXT NOT NULL,             -- JSON
    origin     TEXT NOT NULL,             -- pwa | terminal
    at         TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE pending_writes (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (id),
    tool       TEXT NOT NULL,
    args       TEXT NOT NULL,             -- JSON
    state      TEXT NOT NULL,             -- pending | approved | rejected | failed
    result     TEXT,                      -- JSON
    created_at TEXT NOT NULL
);
