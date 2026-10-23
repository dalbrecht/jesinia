-- SPDX-License-Identifier: AGPL-3.0-or-later
-- zotero_mirror.sqlite (derived): a read-only copy of the Zotero library, rebuilt from Zotero
-- by resyncing from version 0. ``json`` is the serialized Paper. ``attachments`` maps each PDF
-- to its paper and remembers the Zotero full-text version last seen, even when no text came back.
CREATE TABLE papers (
    key TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    json TEXT NOT NULL
);

CREATE TABLE fulltext (
    key TEXT PRIMARY KEY,
    text TEXT NOT NULL
);

CREATE TABLE attachments (
    attachment_key TEXT PRIMARY KEY,
    paper_key TEXT NOT NULL,
    fulltext_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX attachments_paper ON attachments (paper_key);

-- ``children`` maps every other mirrored descendant (notes, child PDFs, annotations) to its paper,
-- so a permanently deleted child can be traced back to the paper that still holds its text.
CREATE TABLE children (
    child_key TEXT PRIMARY KEY,
    paper_key TEXT NOT NULL
);
CREATE INDEX children_paper ON children (paper_key);

CREATE TABLE meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
