-- SPDX-License-Identifier: AGPL-3.0-or-later
-- zotero_mirror.sqlite (derived): a read-only copy of the Zotero library, rebuilt from Zotero
-- by resyncing from version 0. ``json`` is the serialized Paper; ``fulltext.version`` is the
-- newest Zotero full-text version seen among the paper's PDFs.
CREATE TABLE papers (
    key TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    json TEXT NOT NULL
);

CREATE TABLE fulltext (
    key TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
