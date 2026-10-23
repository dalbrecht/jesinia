# Zotero app

Jesinia as a layer over your Zotero library. Zotero stays the home of your papers; Jesinia reads
it through Zotero's local API, makes it searchable next to your notes, and lets your agents read
it.

This is Phase 1:

- **Sync into search:** a read-only mirror of the library (papers, abstracts, notes, annotations,
  PDF text) is kept in `zotero_mirror.sqlite` and indexed into the `zotero` search source, so the
  command palette shows papers and opens them with a `zotero://` link.
- **Read-only MCP tools:** `zotero_search`, `zotero_get` and `zotero_fulltext`.

Phase 2 adds the inbox and write-back (proposed tags and related links). Jesinia never deletes
anything in Zotero.

## Setup

1. Use Zotero 10 or later.
2. In Zotero, open Settings, Advanced, and turn on **Allow other applications on this computer to
   communicate with Zotero**.
3. Authorize Jesinia when Zotero asks, choosing **Always Allow**.

The API key is stored in the macOS Keychain under the service `jesinia-zotero` (ADR 0013), never in
the config file.

## Configuration

The `[zotero]` section of Jesinia's config (also reachable as `zotero.<key>`):

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:23119` | Zotero's local API. Must be `http` on loopback, because the key travels in a header. |
| `sync_interval` | `10m` | How often the mirror is refreshed. |
| `propose_interval` | `4h` | How often Phase 2 will look for proposals. |
| `batch_size` | `15` | Papers per proposal batch (at most 50). |
| `tag_creation` | `generous` | `generous` or `conservative`: how readily new tags are proposed. |

## What is stored

`zotero_mirror.sqlite` is **derived**: a copy of the library that is rebuilt from Zotero at any
time, so it is safe to delete. Zotero remains the source of truth.

## MCP tools

All three are read-only. Everything a paper says (title, authors, tags, abstract, notes, text) is
author-supplied, so it is returned inside an `<untrusted-data>` fence and never in a top-level
field.

- `zotero_search`: search titles, abstracts, notes, annotations and PDF text. Returns each paper's
  `key`, a fixed-text `link` and fenced `content`.
- `zotero_get`: one paper's year, DOI, arXiv id, related keys and link, plus fenced content.
- `zotero_fulltext`: a paper's PDF text one page at a time; pass `next_offset` back as `offset`.

The link text is always "Open in Zotero". Take the paper's title from `content` when citing it.

## Development

```bash
make test        # includes server/tests/apps/zotero
```

Tests run against `FakeZotero` (`jesinia/testing/fake_zotero.py`), an in-process stand-in for the
local API; nothing touches a real Zotero or the Keychain. Layers follow the usual rule:
`domain <- application <- infrastructure <- api`.
