-- SPDX-License-Identifier: AGPL-3.0-or-later
-- notes.sqlite (derived): which session saved which note about which page. Rebuildable
-- from the notes' "About: [[id|Title]]" back-links and provenance comments.
CREATE TABLE saved_notes (
    note_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE INDEX saved_notes_subject ON saved_notes (subject_id);
