-- SPDX-License-Identifier: AGPL-3.0-or-later
-- search.sqlite (derived): full-text index over KB pages and app-registered records.
CREATE VIRTUAL TABLE docs USING fts5(
    source UNINDEXED,
    key UNINDEXED,
    title,
    body,
    url UNINDEXED,
    path,
    meta,
    authoritative UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
