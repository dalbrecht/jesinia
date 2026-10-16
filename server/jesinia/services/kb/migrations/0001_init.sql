-- SPDX-License-Identifier: AGPL-3.0-or-later
-- kb.sqlite (derived): the page index, link graph, and detected conflicts.
CREATE TABLE pages (
    id    TEXT PRIMARY KEY,
    path  TEXT NOT NULL UNIQUE,   -- NFC, relative to the KB root
    disk  TEXT NOT NULL,          -- the path exactly as found on disk
    rev   TEXT NOT NULL,          -- sha256 of the file bytes
    title TEXT NOT NULL,
    mtime REAL NOT NULL
);
CREATE TABLE links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX links_dst ON links (dst);
CREATE TABLE conflicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,     -- duplicate_id | missing
    page_id     TEXT NOT NULL,
    paths_json  TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
