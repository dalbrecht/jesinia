# RSS Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A keyboard-driven, high-volume RSS reader at `/rss/` whose kept posts become archived,
tagged KB pages, plus a KB-wide tag catalog shared by every app and agent.

**Architecture:**
- **Tags** extend the shared `kb` service:
  - a domain grammar, a catalog page `Tags.md`, and index tables;
  - a `TagService` with REST, MCP and an optional agent advisor.
- **RSS** is a new app module, `server/jesinia/apps/rss/`, owning `rss.sqlite`. It polls
  through the foundation `fetch` service and writes kept posts through `kb.write`.
- **The PWA** is `web/apps/rss/`, built from the Notes template, with a chat rail shared from
  `@jesinia/shell`.

**Tech Stack:**
- Python 3.14 / FastAPI / uv;
- `feedparser` (BSD-2) for feeds;
- `trafilatura` (Apache-2.0) for readable-article extraction to Markdown;
- React 19 + `@jesinia/design` / `@jesinia/shell`, vitest, Playwright.

**Spec:** `docs/specs/2026-10-16-rss-reader-design.md`. The foundation spec and
`docs/conventions/` also apply.

## Global Constraints

- **Make is the only entry point.** CI and agents run `make ci`, `make test-e2e` and
  `make icons`, never `uv`/`pnpm` directly. The Python and JS toolchains never orchestrate
  each other.
- **Licensing:** every new source file starts with `SPDX-License-Identifier: AGPL-3.0-or-later`.
  New dependencies are pinned in `uv.lock` / `pnpm-lock.yaml` and must be AGPL-compatible.
- **Layering:** `core ← services ← apps`, and apps never import apps. Inside a module the
  order is domain ← application ← infrastructure ← api (import-linter).
- **Coverage:** Python at 95% per file, with `domain/` at 100%. Web uses vitest thresholds of
  lines/functions/statements 95 and branches 90.
- **KB page format:**
  - Jesinia's front matter lives only under `jesinia:`;
  - tags are `#hashtags` in the body;
  - AI text goes inside `<!-- jesinia:ai … -->` … `<!-- /jesinia:ai -->`;
  - assets go in `_assets/<page-id>/`.
- **Kept post:** `kind: source/rss` with `source`, `feed`, `author`, `published` and
  `captured` (plus `discussion` for HN), written to `folders.source` (default `Sources`).
- **Body order of a kept page:**
  - the author's note;
  - a blank line, then `Tags: #a #b`;
  - the AI takeaway block, only if engaged;
  - `## Article`, then the article Markdown.
- **Derived state stays out of KB files.** Read state, posts and polling state live
  in `rss.sqlite`. Tag and source lookups live in `kb.sqlite`.
- **Tag grammar:**
  - `#` followed by a letter or digit, then letters, digits or `-` (Unicode letters allowed);
  - the `#` must not follow a word character or `/`;
  - fenced code, inline code and heading lines are ignored;
  - matching is case-insensitive; new tags are written lowercase.
- **Tag catalog:** the `Tags.md` page at the KB root, with `jesinia.kind: tag-catalog`.
  - Line format: `- #tag: meaning (aliases: a, b)`; the meaning and aliases are optional.
  - Retired tags carry `(retired)`.
  - Lines Jesinia doesn't recognise are kept byte-for-byte.
- **Near-match rule:** after normalisation (lowercase, spaces→`-`, NFC), a tag is near another
  if any of these hold:
  - they are equal after stripping a final `s`;
  - they are equal after removing `-`/`_`;
  - for tags of 5+ letters, the Levenshtein distance is ≤ 2.
- **Polling:**
  - the first interval is 30 min, then adaptive between 15 min and 12 h;
  - at most 8 concurrent fetches and 1 per host;
  - a feed shows a warning after 3 consecutive failures;
  - feeds are retried with backoff and never dropped.
- **Retention:** unkept posts are pruned 30 days after `published`, or after `first_seen_at`
  when the feed gives no valid date (`rss.retention`, a duration); kept posts are never pruned.
- **Archived images:**
  - at most 20 images and 10 MB per page, saved to `_assets/<page-id>/`;
  - each such folder holds a `.gitignore` containing `*` (images stay local, out of git).
- **Keyboard:**
  - `j`/`k` next/previous, `Enter`/`o` open original, `n` Keep, `e` Engage;
  - `m` read toggle, `Shift+A` mark above read;
  - `g a`/`g i` go to views, `/` search, `?` help;
  - keys are inactive while a text field has focus.
- **Unread counts** show `999+` above 999.
- **UI copy** follows brief §9: plain, sentence case, the cause plus the fix, and no
  exclamation marks.
- **Idempotency (repo rule):** every non-idempotent mutating route (POST/PATCH that writes)
  requires an `Idempotency-Key` header. A repeated key with the same route returns the stored
  status and body without acting again; a missing key is a 400. The PWAs send
  `crypto.randomUUID()` per user action. Built once in Task 7A (`core/idempotency.py`) and used
  by every later mutating route.
- **Design pass (2026-10-16, `docs/design/wireframes/design-pass-2026-10-16.md`):**
  - RSS: Engage and Keep always visible at the reader's top right; Keep is the only way to hold
    a post (no star, no saved-for-later, no `s` key); no on-screen key guide; OPML import and
    export live in Settings → RSS.
  - Settings: an outline of sections on the left, a view pane with a toolbar on top and actions
    along the bottom; new **RSS** section; retired tags can be restored.
- **Every PR:** run the local `make ci` → push → open the PR → `@codex review`. Allow at most
  3 Codex rounds; fix each finding, reply and resolve. Merge with squash only when green.
  Merge commits on stacked branches use `chore(merge): …` titles.

## Review Focus

1. **Huge or hostile feeds:**
   - a 5 MB feed, 10,000 items, a feed redirecting to an internal IP, or invalid XML with a
     valid prefix;
   - expected: parse what's valid, cap item count (newest 500 per poll), never block other
     feeds, and surface the cause on the feed.
   - Test in Task 10: `test_poll_caps_items_and_isolates_a_hostile_feed`.
2. **The same story from many places:** the same article URL in two feeds, or the same GUID
   reposted with an edited title.
   - Expected: one post per feed and GUID. Keeping either writes or updates the single page
     for that `source` URL.
   - Test in Task 12: `test_keep_from_two_feeds_updates_one_page`.
3. **Tags inside code, URLs or headings:** `` `#include` ``, `https://x/#frag`,
   `# Heading`, `C#`.
   - Expected: none of these count as tags, and Rename never rewrites them.
   - Test in Task 1: `test_extract_ignores_code_urls_headings_and_suffix_hashes`.
4. **A hand-edited catalog:** comment lines, a tag listed twice, a tag with stray capitals.
   - Expected: the catalog is parsed leniently and unknown lines are preserved. Duplicates are
     merged on read, and the first meaning wins.
   - Test in Task 2: `test_catalog_parse_is_lenient_and_round_trips_unknown_lines`.
5. **A rename while pages are open or edited:** a page changes between the preview and Apply.
   - Expected: that page is skipped and listed (revision check), and the rest apply.
   - Test in Task 4: `test_rename_skips_pages_changed_since_preview`.

---

## PR 1: KB-wide tags (Tasks 1–7)

### Task 1: Tag grammar and near-matching (domain)

**Files:**
- Create: `server/jesinia/services/kb/domain/tags.py`
- Test: `server/tests/services/kb/domain/test_tags.py`

**Interfaces:**
- Produces:
  - `normalise_tag(raw: str) -> str`: lowercase, spaces/underscores→`-`, NFC, strips a
    leading `#`;
  - `extract_tags(body: str) -> list[str]`: normalised, unique, in order of first appearance;
  - `replace_tag(body: str, old: str, new: str) -> str`: rewrites only grammar-valid
    occurrences of `old` (case-insensitive) to `#new`;
  - `near_matches(tag: str, known: Iterable[str]) -> list[str]`;
  - `TAG_LINE_PREFIX = "Tags:"`;
  - `render_tag_line(tags: Sequence[str]) -> str`, which returns `"Tags: #a #b"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_extract_ignores_code_urls_headings_and_suffix_hashes() -> None:
    body = "# Heading #nope\nUses #SQLite and #wal-mode.\n`#include` x\n```\n#code\n```\nC# and https://x/#frag #2026\n"
    assert extract_tags(body) == ["sqlite", "wal-mode", "2026"]

def test_normalise() -> None:
    assert normalise_tag("#Machine Learning") == "machine-learning"
    assert normalise_tag("sql_lite") == "sql-lite"

def test_replace_tag_rewrites_only_real_tags() -> None:
    body = "See #sqlite, #SQLite3 and `#sqlite`.\n"
    assert replace_tag(body, "sqlite", "databases") == "See #databases, #SQLite3 and `#sqlite`.\n"

def test_near_matches() -> None:
    known = ["sqlite", "databases", "machine-learning", "ml"]
    assert near_matches("SQLite", known) == ["sqlite"]
    assert near_matches("database", known) == ["databases"]
    assert near_matches("sql-lite", known) == ["sqlite"]
    assert near_matches("machine_learnin", known) == ["machine-learning"]
    assert near_matches("ml", known) == ["ml"]
    assert near_matches("rust", known) == []
```

- [ ] **Step 2: Run** `make test-py` (or the pytest file through `make`). Expected: FAIL,
  because the module doesn't exist yet.
- [ ] **Step 3: Implement** the functions in `tags.py` with the grammar from Global
  Constraints. The regex must skip fenced blocks and inline code by masking them first.
  Levenshtein is a small pure function in the same file, so there's no dependency.
- [ ] **Step 4: Run.** Expected: PASS, with `domain/tags.py` at 100% coverage.
- [ ] **Step 5: Commit** `feat(kb): tag grammar and near-matching`.

### Task 2: The tag catalog page (domain)

**Files:**
- Create: `server/jesinia/services/kb/domain/tag_catalog.py`
- Test: `server/tests/services/kb/domain/test_tag_catalog.py`

**Interfaces:**
- Consumes: `normalise_tag` (Task 1).
- Produces:
  - `CatalogEntry(tag: str, meaning: str = "", aliases: tuple[str, ...] = (), retired: bool = False, ignored: bool = False)`,
    a frozen dataclass. An ignored entry is written as `- #tag (ignored)`. It is not an
    approved tag: `resolve` and suggestions skip it, and it is kept only so `uncataloged`
    stops reporting it.
  - `TagCatalog(entries: Mapping[str, CatalogEntry], lines: tuple[str, ...])`, a frozen
    dataclass;
  - `parse_catalog(body: str) -> TagCatalog`;
  - `render_catalog(cat: TagCatalog) -> str`;
  - `TagCatalog.resolve(tag: str) -> str | None`, which matches a tag or an alias and returns
    the canonical tag;
  - `with_entry(cat: TagCatalog, entry: CatalogEntry) -> TagCatalog`;
  - `CATALOG_TITLE = "Tags"`, `CATALOG_KIND = "tag-catalog"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_catalog_parse_is_lenient_and_round_trips_unknown_lines() -> None:
    body = "Intro text\n- #SQLite: SQLite internals (aliases: sql-lite, sqlite3)\n<!-- note -->\n- #sqlite: dup\n- #old-tag (retired)\n"
    cat = parse_catalog(body)
    assert cat.entries["sqlite"] == CatalogEntry("sqlite", "SQLite internals", ("sql-lite", "sqlite3"))
    assert cat.entries["old-tag"].retired
    assert cat.resolve("SQL-Lite") == "sqlite" and cat.resolve("nope") is None
    out = render_catalog(with_entry(cat, CatalogEntry("wal", "write-ahead log")))
    assert "Intro text" in out and "<!-- note -->" in out and "- #wal: write-ahead log" in out
    assert out.count("#sqlite") == 1
```

- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** a line parser that keeps any non-entry line in `lines`, in place.
  The render writes known entries back in their original slots and appends new entries at
  the end.
- [ ] **Step 4: Run.** Expected: PASS, at 100%.
- [ ] **Step 5: Commit** `feat(kb): tag catalog page format`.

### Task 3: Tags and sources in the KB index

**Files:**
- Create: `server/jesinia/services/kb/migrations/0003_tags_sources.sql`
  - `page_tags(page_id, tag)`, primary key (page_id, tag), indexed on tag;
  - `page_sources(page_id PRIMARY KEY, url)`, indexed on url.
- Modify:
  - `server/jesinia/services/kb/application/ports.py` (the `PageIndex` protocol);
  - `server/jesinia/services/kb/infrastructure/` (the SQLite index);
  - `services/kb/application/service.py` (`_store` / `_index_put`) and
    `services/kb/application/scan.py`, wherever `set_links` is called.
- Test: `server/tests/services/kb/test_index_tags.py`

**Interfaces:**
- Consumes: `extract_tags` (Task 1).
- Produces these `PageIndex` methods:
  - `set_tags(id, tags: Iterable[str]) -> None`;
  - `set_source(id, url: str | None) -> None`;
  - `tag_counts() -> dict[str, int]`;
  - `pages_with_tag(tag: str) -> list[PageId]`;
  - `by_source(url: str) -> PageId | None`.

  Every write and scan sets tags from the body and the source from `jesinia.source`.

- [ ] **Step 1: Failing tests:**
  - `test_write_indexes_tags_and_source` writes a page with `#a #b` and `jesinia.source=u`.
    It asserts `tag_counts()=={"a":1,"b":1}` and `by_source(u)==id`.
  - `test_scan_indexes_hand_edited_tags` edits a file on disk, scans, and checks the counts
    follow.
  - `test_reserved_folders_are_not_indexed` checks a `Journal/x.md` with `#a` doesn't count.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the migration, the index methods, and the calls next to every
  `set_links` call.
- [ ] **Step 4: Run.** Expected: PASS. `make ci` stays green, because `jesinia reindex`
  rebuilds the new tables.
- [ ] **Step 5: Commit** `feat(kb): index page tags and sources`.

### Task 4: `TagService`, REST and MCP

**Files:**
- Create:
  - `server/jesinia/services/kb/application/tags.py`;
  - `server/jesinia/services/kb/api/tags.py` (router and tool builders).
- Modify:
  - `services/kb/module.py` registers the router and tools and exposes
    `ctx.services["tags"]`;
  - `web/packages/client/openapi.json` and the generated types (via `make gen-client`).
- Test: `server/tests/services/kb/test_tags_service.py`, `test_tags_api.py`

**Interfaces:**
- Consumes:
  - from Tasks 1–3: `KbService`, the `PageIndex` tag and source methods, and the catalog
    functions;
  - from Task 5, optionally: an advisor with
    `async suggest(text: str, catalog: list[CatalogEntry]) -> list[str]` and
    `async same_meaning(tag: str, catalog: list[CatalogEntry]) -> str | None`.
- Produces `TagService(kb, advisor: TagAdvisor | None)` with:
  - `async catalog() -> list[TagInfo]`, where
    `TagInfo(tag, meaning, aliases, retired, count)`;
  - `async check(raw: str) -> TagCheck`, where
    `TagCheck(normalised: str, exists: bool, canonical: str | None, near: list[str], same_meaning: str | None)`;
  - `async suggest(text: str, *, limit: int = 5) -> list[Suggestion]`, where
    `Suggestion(tag, new: bool)`. Catalog matches come first, then advisor tags; retired
    tags are never suggested.
  - `async add(tag: str, meaning: str = "") -> None`, which raises `TagExists` /
    `NearDuplicate(near: list[str])` unless given `force=True` (an explicit user override);
  - `async uncataloged() -> list[tuple[str, int]]`;
  - `async preview_rename(old: str, new: str) -> RenamePreview`, where
    `RenamePreview(pages: list[PageRef], skipped_reserved: list[str])` and
    `PageRef(id, title, rev)`;
  - `async apply_rename(old: str, new: str, revs: Mapping[PageId, str]) -> RenameResult`,
    where `RenameResult(updated: list[PageId], skipped_changed: list[PageId])`;
  - `async merge(sources: list[str], into: str, revs) -> RenameResult`;
  - `async retire(tag: str) -> None`;
  - `async adopt(tag: str) -> None`;
  - `async ignore(tag: str) -> None`, which records `- #tag (ignored)` in the catalog so
    `uncataloged()` stops listing it.

  The catalog page is created on first `add` (title `Tags`, folder root, `jesinia.kind:
  tag-catalog`). A rename adds `old` as an alias of `new`.
- REST under `/api/kb/tags`:
  - `GET` catalog;
  - `GET /check?tag=`;
  - `POST /suggest {text}`;
  - `POST` add;
  - `GET /uncataloged`;
  - `POST /rename/preview`, `POST /rename`, `POST /merge`, `POST /retire`, `POST /adopt`, `POST /ignore`.
- MCP (read-only, `writes=False`): `tags_list`, `tags_suggest {text}`, `tags_check {tag}`.

- [ ] **Step 1: Failing tests:**
  - `test_add_refuses_near_duplicates_unless_forced` (adding `SQLite` when `sqlite` exists
    raises `TagExists`; `database` vs `databases` raises `NearDuplicate`);
  - `test_suggest_prefers_catalog_and_flags_new`;
  - `test_rename_rewrites_pages_and_adds_alias`;
  - `test_rename_skips_pages_changed_since_preview`;
  - `test_rename_leaves_reserved_folders_and_reports_them`;
  - `test_uncataloged_lists_hashtags_missing_from_catalog`;
  - `test_ignored_tags_leave_uncataloged_and_are_never_suggested`;
  - `test_tools_are_read_only`;
  - one API test per route, covering status codes and the 409 on `NearDuplicate` with
    `near` in the body.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Writes go through `KbService.write` / `update_body` with base
  revisions. Reserved paths come from `kb/domain/paths.is_reserved`, scanning the root's
  reserved folders read-only only to list them.
- [ ] **Step 4: Run.** Expected: PASS at 95%+, then `make gen-client`.
- [ ] **Step 5: Commit** `feat(kb): tag catalog service, REST and MCP tools`.

### Task 5: One-shot agent asks and the tag advisor

**Files:**
- Modify:
  - `server/jesinia/services/agents/application/sessions.py` adds `one_shot`;
  - `server/jesinia/testing/fake_runner_app.py`: for prompts starting `TAGS:`, the fake
    runner replies `["fixture","new-fixture-tag"]`.
- Create: `server/jesinia/services/kb/infrastructure/agent_advisor.py`.
- Test: `server/tests/services/agents/test_one_shot.py`,
  `server/tests/services/kb/test_agent_advisor.py`

**Interfaces:**
- Produces:
  - `SessionService.ask(session_id: str, prompt: str, *, timeout: float = 45.0) -> str`. It
    sends one turn to an existing session (resuming its agent context), waits for its
    `TurnDone`, and returns that turn's concatenated text. It raises `SessionNotFound` /
    `TimeoutError`.
  - `SessionService.one_shot(runner: str, pack: ContextPack, prompt: str, *, timeout: float = 45.0) -> str`,
    built on `create` + `ask` + close.
    It creates a session with no tools allowed, runs one turn, returns the concatenated text,
    and closes the session. It raises `RunnerUnavailable` / `TimeoutError`.
  - `AgentTagAdvisor(sessions, runner: Callable[[], str | None])`, which implements the
    Task 4 advisor protocol:
    - the text goes in as `UntrustedDoc`;
    - the catalog goes in the role prompt;
    - the reply is parsed as a JSON array of tags;
    - anything unparsable gives `[]`.
- `kb/module.py` wires the advisor with a lazy lookup of `ctx.services["agents"]` at call
  time, because `kb` starts before `agents` in `registry.py`. The runner is the first
  available one, preferring `claude-code`.

- [ ] **Step 1: Failing tests:**
  - `test_one_shot_returns_text_and_closes_session` (FakeRunner);
  - `test_one_shot_times_out`;
  - `test_ask_continues_an_existing_session`, which asserts the turn carries the session's
    resume id;
  - `test_advisor_parses_json_and_ignores_garbage`;
  - `test_advisor_treats_post_text_as_untrusted`, which asserts the prompt contains the
    `<untrusted-data` fence.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(agents): one-shot asks; tag advisor`.

### Task 6: Settings → Tags

**Files:**
- Create: `web/apps/launcher/src/settings/Tags.tsx`, plus its test
  `web/apps/launcher/src/__tests__/tags.test.tsx`.
- Modify: the Settings page section list in `web/apps/launcher/src/`, adding a "Tags"
  section with anchor `#tags`.

**Interfaces:**
- Consumes: the Task 4 REST routes through `@jesinia/client`.
- **UI:**
  - a table of tag, meaning, aliases, page count and status, with a filter field and sort by
    name or count;
  - row actions: Rename, Merge into…, Retire;
  - an **Uncataloged** panel with Adopt, Merge into… and Ignore;
  - Rename and Merge open a preview dialog. It lists the pages to update and those **not
    updated (private folder)**, and has Apply.
  - After Apply, a toast: "Renamed #a to #b in N pages." If any page was skipped: "… N
    changed since the preview and were skipped."

- [ ] **Step 1: Failing tests (vitest, fakeClient):**
  - lists the catalog with counts;
  - the rename preview shows reserved pages as not updated, and Apply posts the revisions;
  - a 409 near-duplicate on add shows "Use #sqlite?" with a one-click accept;
  - adopt and ignore from the Uncataloged panel.
- [ ] **Step 2: Run** `make test-js`. Expected: FAIL.
- [ ] **Step 3: Implement** with `@jesinia/design` components only (Table/List, Dialog,
  Button, Input, Badge).
- [ ] **Step 4: Run.** Expected: PASS, with launcher thresholds met.
- [ ] **Step 5: Commit** `feat(launcher): Settings → Tags`.

### Task 7: Tag chips on Save note, Pigeonpile compatibility, and docs

**Files:**
- Create: `web/packages/shell/src/TagPicker.tsx`, plus its tests.
- Modify:
  - `web/apps/notes/src/Chat.tsx` (Save note sends `tags`);
  - `server/jesinia/apps/notes/application/service.py`;
  - `docs/design/compat/fixture-kb/`;
  - `docs/adr/0007-pigeonpile-compat-results.md`;
  - `docs/conventions/kb-format.md`;
  - the skills `jesinia-kb-conventions` and the Codex snippet.

**Interfaces:**
- **`TagPicker`:**
  - `TagPicker({ text: string; value: string[]; onChange: (tags: string[]) => void })`;
  - it calls `/api/kb/tags/suggest` and `/check`;
  - new tags show a *new* badge;
  - a near-match shows "Use #x?";
  - it is exported from `@jesinia/shell`. Task 14 reuses it.
- **Notes:** `NotesService.save_note(..., tags: Sequence[str] = ())` writes
  `render_tag_line` after the user text. A tag accepted as *new* is added to the catalog
  first through `TagService.add`.

- [ ] **Step 1: Failing tests:**
  - `TagPicker` (suggestions render, new badge, near-match accept);
  - `test_save_note_writes_tag_line_and_catalogs_new_tags`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Compatibility fixture.** Add a fixture page `Ideas/Tag grammar.md` using
  `#SQLite #sqlite #wal-mode #2026 #ünïcode`, and an ADR 0007 addendum row "tag grammar"
  marked *pending manual check*.
  - The author confirms in the Wiki whether case folds and whether hyphens, digits and
    Unicode are accepted.
  - Narrow the grammar if they aren't, with a follow-up issue if needed.
- [ ] **Step 5: Docs.**
  - `kb-format.md`: the tag grammar, the `Tags:` line, the catalog page.
  - Skills: agents use `tags_suggest` / `tags_check` before writing tags, and never invent
    near-duplicates.
- [ ] **Step 6: Run** `make ci`. Expected: PASS. **Commit**
  `feat(notes): tag suggestions on Save note; tag docs`.

### Task 7A: Idempotency keys and tag restore

**Files:**
- Create: `server/jesinia/core/idempotency.py`, `server/jesinia/core/migrations/` (or the
  module DB helper's equivalent) for an `idempotency(key, route, status, body, created_at,
  PRIMARY KEY(key, route))` table in each owning module's database.
- Modify: the tag mutation routes (`/api/kb/tags` add, rename, merge, retire, adopt, ignore);
  `TagService` (`restore(tag)`); `web/apps/launcher/src/settings/Tags.tsx` (Restore button on
  retired rows; every mutating call sends an `Idempotency-Key`); `web/packages/client` helper
  `withIdempotency()`.
- Test: `server/tests/core/test_idempotency.py`, extended tag API tests, `tags.test.tsx`.

**Interfaces:**
- **`idempotent(store) -> Depends`:** a FastAPI dependency plus response hook. It requires
  `Idempotency-Key` (1–200 chars), looks up `(key, route)`, and returns the stored response if
  present; otherwise it runs the handler and stores the status and JSON body. Keys older than
  7 days are pruned daily.
- **`TagService.restore(tag) -> None`** clears `retired`; `POST /api/kb/tags/restore {tag}`.

- [ ] **Step 1: Failing tests:**
  - `test_repeated_key_returns_stored_response_without_acting`;
  - `test_missing_key_is_400`;
  - `test_same_key_different_route_is_independent`;
  - `test_rename_retry_does_not_rewrite_twice`;
  - `test_restore_unretires_a_tag`;
  - web: mutating calls send a key; Restore shows on retired rows.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make ci`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): idempotency keys; restore retired tags`.
- [ ] **Step 6: Open PR 1** (`feat(kb): KB-wide tag catalog`), then the Codex loop and merge.

## PR 2: RSS backend (Tasks 8–12)

### Task 8: Feed parsing and autodiscovery

**Files:**
- Create: the package skeleton `server/jesinia/apps/rss/{__init__,infrastructure/__init__}.py`
  and `apps/rss/infrastructure/parse.py`, plus fixtures in
  `server/tests/apps/rss/fixtures/` (rss2, atom, jsonfeed, broken, html-with-link-alternate,
  hn).
- Modify: `server/pyproject.toml` (add `feedparser`).

**Interfaces:**
- **`parse_feed(body: bytes, url: str) -> ParsedFeed`:**
  - `ParsedFeed(title, site_url, items: list[ParsedItem])`, where
    `ParsedItem(guid, url, title, author, published_at, snippet, content_html, discussion_url, is_summary)`;
  - `is_summary` is true when the item has no full content (`content:encoded` / Atom
    `content`) and only a summary/description;
  - `published_at` is `None` when the feed's date is missing or invalid;
  - the GUID falls back to `url + title`;
  - `snippet` is plain text of at most 280 characters;
  - `content_html` is sanitised with the `fetch` allowlist (`nh3`);
  - items are capped at the newest 500.
- **`discover(html: bytes, base: str) -> str | None`:** finds `<link rel=alternate>` with an
  RSS, Atom or JSON Feed type.
- **HN items** (`news.ycombinator.com` feeds): `url` is the linked article and
  `discussion_url` is the comments link.

- [ ] **Step 1: Failing tests:** one per fixture, plus
  `test_parse_caps_items_and_sanitises_content` and `test_hn_items_link_article_and_discussion`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(rss): feed parsing and discovery`.

### Task 9: The `rss` module, schema, feeds and OPML

**Files:**
- Create:
  - `server/jesinia/apps/rss/{__init__,module,api}.py`;
  - `apps/rss/domain/{feed,opml}.py`;
  - `apps/rss/application/feeds.py`;
  - `apps/rss/infrastructure/store.py`;
  - `apps/rss/migrations/0001_init.sql`.
- Modify: `server/jesinia/registry.py` (add `RssModule` after `NotesModule`, and `RSS_DB` to
  `DATABASES`), and the import-linter contracts.
- Test: `server/tests/apps/rss/{test_opml,test_feeds_api}.py`

**Interfaces:**
- **`RSS_DB = ModuleDb("rss", DbClass.HISTORY, …)`.** It holds read state, which is not
  rebuildable, so it is included in backup.
- **Schema:**
  - `folders(id, name UNIQUE, position)`;
  - `feeds(id, url UNIQUE, title, site_url, folder_id NULL, interval_s, next_poll_at,
    etag, last_modified, fail_count, last_error, last_ok_at)`;
  - `posts(id, feed_id, guid, url, title, author, published_at, snippet, content_html,
    discussion_url NULL, is_summary, first_seen_at, full_md NULL, read, kept_page_id NULL,
    archive_state NULL,
    archive_error NULL,
    UNIQUE(feed_id, guid))`;
  - indexes on `(read, published_at)` and on `url`.
- **Domain:**
  - `parse_opml(xml: str) -> list[OpmlFeed]`, where `OpmlFeed(url, title, folder)`; one
    folder level, deeper levels flattened into their top folder;
  - `render_opml(feeds) -> str`.
- **`FeedService` (application):**
  - `add(url: str, folder: str | None) -> Feed`, using autodiscovery from Task 8's parser;
  - `remove(id)`;
  - `move(id, folder)`;
  - `rename_folder`;
  - `import_opml(xml) -> ImportResult(added, existing, failed)`;
  - `export_opml() -> str`;
  - `list() -> list[FeedView]`, where
    `FeedView(id, title, url, folder, unread, warning: str | None)`.
- **REST `/api/rss`:**
  - `GET /feeds`, `POST /feeds`, `DELETE /feeds/{id}`, `PATCH /feeds/{id}`;
  - `POST /opml` (upload), `GET /opml` (used by Settings → RSS);
  - every POST/PATCH/DELETE here uses the Task 7A idempotency dependency;
  - `PATCH /folders/{id}`.

- [ ] **Step 1: Failing tests:**
  - `test_opml_round_trip_keeps_one_folder_level`;
  - `test_import_opml_skips_existing_and_reports_failures`;
  - API tests for each route (auth required, 404/409 shapes).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(rss): module, feeds and OPML`.

### Task 10: Polling

**Files:**
- Create: `apps/rss/application/poller.py`.
- Modify:
  - `apps/rss/module.py`: schedule `rss.poll` every 60 s, which polls feeds that are due;
  - `server/jesinia/testing/fake_runner_app.py`: construct `FetchService` with a resolver
    that allows loopback only under the throwaway `JESINIA_HOME` guard, so e2e can serve
    fixture feeds.
- Test: `server/tests/apps/rss/test_poller.py`

**Interfaces:**
- `Poller(store, fetch: FetchService, parse, clock)`:
  - `async poll_due() -> PollSummary`;
  - `async poll_all() -> PollSummary` (manual Refresh, `POST /api/rss/refresh`).
- **Adaptive interval:**
  - after a poll with new items, interval = max(15 min, interval / 2);
  - with none, interval = min(12 h, interval × 1.5);
  - the first interval is 30 min.
- **Failures:**
  - `fail_count += 1`;
  - the next poll is at `min(12 h, 30 min × 2^fail_count)`;
  - `last_error` holds the cause;
  - at `fail_count >= 3` the feed shows `warning`.
- **Concurrency:** `asyncio.Semaphore(8)` globally, plus a host-scoped `asyncio.Semaphore(1)`
  held around each complete fetch, so at most one request per host is in flight (fetch's
  `_pace` only spaces request starts). The
  `ETag`/`Last-Modified` values are passed through.

- [ ] **Step 1: Failing tests (fake clock, fake fetch):**
  - `test_interval_adapts_between_bounds`;
  - `test_failures_back_off_and_warn_after_three`;
  - `test_poll_caps_items_and_isolates_a_hostile_feed` (a 5 MB body hits the `FetchTooLarge`
    cause; a blocked internal redirect hits `FetchBlocked`; one feed's error doesn't stop the
    others);
  - `test_new_items_dedupe_by_guid_per_feed`;
  - `test_refresh_polls_everything_now`;
  - `test_one_request_in_flight_per_host` (two feeds on one slow host never overlap).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(rss): adaptive polling`.

### Task 11: Posts, views, retention, full article, MCP reads

**Files:**
- Create:
  - `apps/rss/application/posts.py`;
  - `server/jesinia/services/fetch/infrastructure/extract.py`.
- Modify: `server/pyproject.toml` (add `trafilatura`) and `apps/rss/api.py`.
- Test: `server/tests/apps/rss/test_posts.py`, `server/tests/services/fetch/test_extract.py`

**Interfaces:**
- **Fetch:**
  - an application port in `services/fetch/application/ports.py`:
    `ArticleExtractor` Protocol with `extract(html: bytes, url: str) -> Article | None`,
    where `Article(title, markdown, image_urls: list[str])` is also defined there;
  - the infrastructure implementation `TrafilaturaExtractor` in
    `services/fetch/infrastructure/extract.py`, using trafilatura's Markdown output with
    images and links;
  - `FetchService(..., extractor: ArticleExtractor | None = None)` gains
    `article(url) -> Article | None`, composing `get` and the injected extractor. `FetchModule`
    injects `TrafilaturaExtractor`, so the application layer never imports infrastructure.
- **`PostService`:**
  - `list(view: Literal["unread","kept","all"], feed_id: int | None, folder_id: int | None, before: tuple[str,int] | None, limit: int = 100) -> Page[PostView]`,
    keyset-paginated newest first;
  - `set_read(ids, read: bool)`;
  - `mark_read_above(post_id, view, feed_id, folder_id)`;
  - `full_article(id) -> str`, which caches `full_md` and raises `ArticleUnavailable(cause)`;
  - `search(q, limit=50)`, a `LIKE` search over title and snippet of retained posts;
  - `prune(now)`, which removes posts older than the retention window (by `published_at`, else
    `first_seen_at`) that aren't kept.
  - The prune job runs daily.
- **REST:**
  - `GET /api/rss/posts?view&feed&folder&before`;
  - `PATCH /posts/{id}`;
  - `POST /posts/read-above`;
  - `GET /posts/{id}/article`;
  - `GET /posts/search?q`;
  - `GET /counts`, which returns per-feed and per-folder unread counts plus the view counts.
- **MCP (read-only):** `rss_search_posts {q}`, `rss_get_post {id}`. The post text is returned
  marked as untrusted source text.

- [ ] **Step 1: Failing tests:**
  - `test_views_and_keyset_pagination`;
  - `test_mark_read_above_respects_the_current_view`;
  - `test_prune_keeps_kept_and_prunes_undated_by_first_seen`;
  - `test_full_article_caches_and_reports_cause`;
  - `test_extract_article_returns_markdown_and_images` (from a fixture HTML);
  - `test_mcp_tools_are_read_only`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(rss): posts, retention and full articles`.

### Task 12: Keep, archive, Engage

**Files:**
- Create: `apps/rss/application/keep.py`, `apps/rss/application/engage.py`.
- Modify: `apps/rss/api.py`, `core/config.py` (`folders.source` default `Sources`,
  `rss.retention` default `30d` — extend `parse_duration` with `d`).
- Test: `server/tests/apps/rss/test_keep.py`, `test_engage.py`

**Interfaces:**
- Consumes:
  - `KbService.write`, `PageIndex.by_source` (Task 3);
  - `TagService.add` / `check` (Task 4);
  - `SessionService.create` / `ask` (Task 5);
  - `FetchService.article` / `get` (Task 11);
  - `wrap_ai`, `inert`, `provenance_label`.
- **`KeepService.keep(post_id, note: str, tags: list[str], takeaway: Takeaway | None) -> KeepResult`:**
  - `Takeaway(text, agent, model, session)`;
  - `KeepResult(page_id, path, created: bool)`;
  - it writes the page immediately (body order from Global Constraints), using `full_md` if
    cached, else the sanitised feed content converted to Markdown with the note "Archived
    from the feed; the full article wasn't available.";
  - new tags are catalogued first.
  - **An existing page for the same source:** append `\n\n_Kept again {date}:_ {note}`
    after the note block, merge tags into the `Tags:` line, and leave the article
    untouched.
  - Then, **only for a newly created page or one whose archive isn't done**, it sets
    `archive_state = "archiving"` and schedules `archive(page_id, post_id)`. Keeping again never
    re-archives, so later edits to the archived article are preserved.
- **Idempotency:** Keep, Engage, takeaway and archive retry use the Task 7A dependency (no
  separate `keep_requests` table).
- **Recovery:** on module start, every post with `archive_state = "archiving"` is
  rescheduled, so a restart mid-archive resumes instead of showing "Archiving…" forever.
- **`archive`:**
  - fetches the full article if not cached;
  - downloads up to 20 images and 10 MB in total into `_assets/<page-id>/`, with
    `.gitignore` = `*`;
  - rewrites image links to the relative paths;
  - updates the page through `kb.write` with the base revision. On a revision conflict it
    retries once against the fresh page, replacing only the `## Article` section.
  - It sets `archive_state` to `done`, or to `failed` with `archive_error`. Retry is
    `POST /posts/{id}/archive`.
- **`EngageService`:**
  - `start(post_id, runner) -> Session`, with a ContextPack role prompt about discussing the
    post and the post as `UntrustedDoc(source=url)`. Session subject:
    `Subject("rss", str(post_id), title)`.
  - `takeaway(session_id) -> Takeaway`, which uses `SessionService.ask` on the *engaged*
    session ("1–3 sentence takeaway"), so the agent summarises the conversation it actually
    had. It is returned for the author to edit, not written.
- **REST:**
  - `POST /api/rss/posts/{id}/keep {note, tags, takeaway?}`;
  - `POST /posts/{id}/engage {runner}`;
  - `POST /api/rss/sessions/{id}/takeaway`.

- [ ] **Step 1: Failing tests:**
  - `test_keep_writes_source_page_in_spec_order`, which asserts the front matter keys, the
    `Tags:` line, and `## Article`;
  - `test_keep_from_two_feeds_updates_one_page`;
  - `test_keep_again_appends_note_and_merges_tags`;
  - `test_archive_saves_images_with_gitignore_and_caps`;
  - `test_archive_failure_is_visible_and_retryable`;
  - `test_keep_retry_with_same_idempotency_key_writes_once`;
  - `test_stranded_archiving_posts_resume_on_start`;
  - `test_hn_keep_records_discussion`;
  - `test_keep_again_does_not_rearchive`;
  - `test_takeaway_is_returned_not_written`;
  - `test_engaged_keep_wraps_takeaway_in_provenance`;
  - `test_keep_is_refused_while_the_kb_is_unavailable` (a 503 with the KB's cause, and
    nothing queued).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make ci`. Expected: PASS, then `make gen-client`.
- [ ] **Step 5: Commit** `feat(rss): keep, archive and engage`. **Open PR 2**
  (`feat(rss): reader backend`), stacked on PR 1, then the Codex loop and merge.

## PR 3: RSS app (Tasks 13–15)

### Task 13: PWA scaffold, three panes, keyboard

**Files:**
- Create: `web/apps/rss/` from the Notes template:
  - `index.html` with favicon links;
  - `public/manifest.webmanifest`;
  - `src/{App,Feeds,PostList,Reader,keys}.tsx`;
  - `src/api.ts` and tests.
- Modify:
  - `web/scripts/export-icons.mjs` (`SHIPPED` gains `web/apps/rss/public/icons`) and run
    `make icons`;
  - `web/packages/shell/src/apps.ts` (RSS `built: true`);
  - the Vite build output into `server/jesinia/static/rss/`.

**Interfaces:**
- **Keyboard:** `useKeys(map: Record<string, () => void>)` in `src/keys.ts` with the exact
  map from Global Constraints. `g` chords time out after 1 s, and keys are ignored when
  `document.activeElement` is an input, textarea or contenteditable.
- **`PostList`:**
  - virtualised rows (react-window is not allowed; implement a simple windowed list);
  - selection drives the Reader;
  - selecting marks the post read via `PATCH`.
- **`Feeds`:**
  - shows views and folders with counts capped `999+`;
  - collapsible folders, with collapse remembered in `localStorage` inside try/catch;
  - a warning icon with the cause in a tooltip.
- **Narrow layout (under 768 px), per the wireframe:** the list fills the screen, a post
  opens the reader full-screen with a back button, and the chat opens as a bottom sheet.
- **`Reader`:**
  - the serif reading style (`@jesinia/design` `read` class) and a byline;
  - a toolbar with **Open original ↗**, **Engage** and **Keep** always visible at the top
    right, and Previous / Next / Mark unread along the bottom (design pass); no key guide;
  - a **Load full article** button when `is_summary` is true, or `content_html` is short (under 1,500 characters), or
    the feed marks it as a summary;
  - an "Archiving…" / "In KB" / "Archive incomplete — Retry" status.

- [ ] **Step 1: Failing tests:**
  - the key map, covering every key, focus suppression and the `g` chord;
  - `999+`;
  - selecting marks read;
  - `Shift+A` calls read-above with the current view;
  - the narrow layout switches list → reader → back;
  - Load full article shows its cause on failure;
  - `icons.test.ts`, copied from Notes with `BASE="/rss"`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make ci`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(rss): reader app, three panes and keys`.

### Task 14: Keep popover, Engage rail, shared chat

**Files:**
- Create:
  - `web/apps/rss/src/KeepPopover.tsx`;
  - `web/packages/shell/src/chat/{ChatRail.tsx,events.ts}`, moved from Notes `Chat.tsx` and
    `events.ts` with Notes updated to import them.
- Modify: `web/apps/notes/src/*` (imports only).
- Test: the existing Notes chat tests move with the code, plus new RSS tests.

**Interfaces:**
- **`ChatRail({ session, onSessionNeeded, footer })`.** It's the Notes chat minus Save note.
  Notes passes its Save note button as `footer`.
- **`KeepPopover`:**
  - fields: `{ note, tags (TagPicker), takeaway? }`;
  - `⌘↵` keeps and `Esc` cancels;
  - it shows "Kept to Sources/…" on success, and the cause on failure;
  - with no runner available, the tag area says "Suggestions from your catalog only (no
    agent available)."
- **"Keep with this conversation":** calls `/takeaway`, then opens `KeepPopover` with the
  takeaway editable and the note prefilled from the last user message.

- [ ] **Step 1: Failing tests:**
  - `n` opens the popover focused on the note;
  - `⌘↵` posts note, tags and takeaway;
  - near-match acceptance through TagPicker;
  - Engage opens the rail and creates a session;
  - Keep with conversation shows an editable takeaway;
  - the Notes chat tests pass unchanged after the move.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make ci`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(rss): keep and engage`.

### Task 15: Feed management, e2e, docs, acceptance

**Files:**
- Create:
  - `web/apps/launcher/src/settings/Rss.tsx` (Settings → RSS: OPML import/export, retention,
    kept-posts folder), and convert the launcher Settings page to the design-pass layout:
    an outline of sections on the left, a view pane with a toolbar on top and actions at the
    bottom (existing sections move in unchanged);
  - `web/apps/rss/src/ManageFeeds.tsx`, covering add (URL or site),
    folders, and the per-feed error with Retry;
  - `web/tests/e2e/rss.spec.ts`;
  - fixture feeds under `web/tests/e2e/fixtures/feeds/`, served by `make e2e-server`.
- Modify:
  - `Makefile` (`e2e-server` serves the feed fixtures on a second loopback port);
  - `docs/operations.md` (RSS, OPML, retention, archived images stay local);
  - `README.md` (apps list);
  - `docs/conventions/adding-an-app.md` if the template changed;
  - the skills (`rss_search_posts`, `rss_get_post`; no keep tool).

**Interfaces:**
- **e2e flows:**
  1. Add the fixture feed, see 3 posts, `j`/`j`, then `n`. Type a note, accept the suggested
     tag `fixture`, then `⌘↵`. Open Notes, search "fixture", and see the Sources page.
  2. Settings → Tags: rename `#fixture` to `#fixtures`, preview 1 page, Apply. The page body
     has `#fixtures`.
  3. In Settings → RSS, import an OPML with 2 feeds into a folder; back in RSS, see the folder
     with counts.

- [ ] **Step 1: Write the e2e spec and the ManageFeeds vitest tests.** Expected: FAIL.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Run** `make ci` and `make test-e2e`, including
  `GIT_CONFIG_GLOBAL=/dev/null make test-e2e`. Expected: PASS.
- [ ] **Step 4: Commit** `feat(rss): feed management, e2e and docs`.
- [ ] **Step 5: Open PR 3** (`feat(rss): reader app`), then the Codex loop and merge.
- [ ] **Step 6: Acceptance on the author's Mac** (the auto-updated install):
  1. import the author's OPML or add HN plus a few blogs;
  2. keep one post quickly and one through Engage;
  3. check both pages in Notes and the Pigeonpile Wiki;
  4. check that the archived images aren't in `git -C ~/kb status`;
  5. complete the tag-grammar row in ADR 0007.

  Record the results in the spec, and set its status → Implemented.
