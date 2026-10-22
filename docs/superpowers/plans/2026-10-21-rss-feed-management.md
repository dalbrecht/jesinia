# RSS Feed Management, Bulk Mark-Read and Auto Full Article Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bulk "mark read" at feed / folder / everything level, move Refresh into the list header, take sync errors out of the reading view into a full-screen feed management page (add, move, remove, sync, OPML, per-feed retention), and load full articles automatically.

**Architecture:** Server: two new post routes (scoped mark-read returning the marked ids, and bulk set-read for Undo), a migration adding `newest_post_at` and a per-feed retention override to `feeds`, a per-feed prune, and a retention route; `FeedOut` grows the health fields. Web: the design-system Toast gains a timed action and a close callback (for Undo), the list header gains Mark all read and ↻, the sidebar loses warnings and gains hover Mark read, `Reader` auto-loads the article, and `ManageFeeds` becomes a routed full screen at `/rss/manage`.

**Tech Stack:** Python 3 / FastAPI / SQLite (`server/jesinia/apps/rss`), pytest; React 19 + TypeScript, vitest + Testing Library, Playwright e2e (`web/apps/rss`, `web/packages/design`); typed client from OpenAPI (`make gen-client`).

**Spec:** `docs/specs/2026-10-21-rss-feed-management-design.md` (amends `docs/specs/2026-10-16-rss-reader-design.md`).

## Global Constraints

- Every source file keeps its `SPDX-License-Identifier: AGPL-3.0-or-later` header (`make spdx`).
- Python: mypy `--strict`, ruff, and the per-file coverage gate ≥ 95% (`make coverage-check`).
- Every mutating RSS route takes the idempotency header via `IdempotentCall = keyed`, like the existing ones in `api.py`.
- After any server API change: `make gen-client` regenerates `web/packages/client/openapi.json` and `src/openapi.d.ts`; commit them with the change.
- Errors shown to the user: the cause, then what to do (RSS spec §9 / existing `fail(...)` helper).
- Kept posts are never pruned and never change kept state when marked read.
- Mark read only touches posts with `first_seen_at <= as_of` (spec §2.2).
- Retention: days 1–365 (`RETENTION_DAYS_MIN`/`MAX`); posts cap 10–10 000 (builder's call, `RETENTION_POSTS_MIN = 10`, `RETENTION_POSTS_MAX = 10_000`). A posts cap only ever deletes **read** posts.
- Copy, verbatim from the spec: "✓ Mark all read", "Mark read", "↻ Refresh", "⚙ Manage feeds", "‹ Back to reading", "+ Add feed", "Sync all", "Sync now", "Remove", "Import OPML", "Export OPML", "All", "Failing", "Showing full article · Show feed text", "Marked {n} in {scope} read" with "Undo".
- Don't add a confirm dialog anywhere in this work.

## Review Focus

1. **Bankruptcy on a huge backlog:** marking >32 766 posts read, then Undo, must not hit SQLite's bound-variable limit. Test in Task 1 (`test_set_read_chunks_beyond_the_variable_limit`).
2. **Fast `j`/`k`:** an article that resolves after the user moved on must not render on the next post or warn about state on an unmounted component. Test in Task 7 (`ignores an article that arrives after moving on`).
3. **Marking a folder read from the sidebar while a feed inside it is open:** the list on screen must reload, not keep stale unread rows. Test in Task 6 (`sidebar mark read on the folder in view reloads the list`).
4. **Switching a busy feed to a posts cap when nothing's been read:** nothing gets deleted. Test in Task 3 (`test_posts_cap_never_prunes_unread`).
5. **Removing the feed in view from the management screen, then going back:** the reader lands on All unread, not an empty dead scope. And if Undo is pressed, nothing is unsubscribed. Test in Task 8 (`remove waits for the toast and undo keeps the feed`).

---

### Task 1: Server — scoped mark-read and bulk set-read

**Files:**
- Modify: `server/jesinia/apps/rss/infrastructure/store.py` (`SqlitePostStore`)
- Modify: `server/jesinia/apps/rss/application/ports.py` (`PostStore` protocol)
- Modify: `server/jesinia/apps/rss/application/posts.py` (`PostService`)
- Modify: `server/jesinia/apps/rss/api.py`
- Test: `server/tests/apps/rss/test_posts.py`

**Interfaces:**
- Produces:
  - `SqlitePostStore.mark_unread_read(feed_id: int | None, folder_id: int | None, as_of: datetime) -> list[int]`: marks every `read = 0` post in scope with `first_seen_at <= as_of` as read and returns their ids (ascending). Use `_scope("unread", feed_id, folder_id)`.
  - `SqlitePostStore.set_read(ids, read)` now chunks `ids` into batches of `_SQL_VARS = 900` inside one transaction; same signature.
  - `PostService.mark_all_read(feed_id: int | None, folder_id: int | None, as_of: datetime) -> list[int]`
  - `POST /api/rss/posts/mark-read`, body `MarkReadIn {feed: int | None = None, folder: int | None = None, as_of: str}` → `MarkedIdsOut {marked: int, ids: list[int]}`. Both `feed` and `folder` null means everything.
  - `POST /api/rss/posts/read-state`, body `ReadStateIn {ids: list[int] (max_length=MAX_READ_IDS = 100_000), read: bool}` → `MarkedOut {marked: int}` (existing model).

- [ ] **Step 1: Write the failing tests** in `test_posts.py`:

```python
def test_mark_all_read_by_feed_folder_and_everything(world: World) -> None:
    a = world.feed("https://a.example/feed", folder="Tech")
    b = world.feed("https://b.example/feed", folder="Tech")
    c = world.feed("https://c.example/feed")
    world.add(a, _item("a1", _day(1)))
    world.add(b, _item("b1", _day(1)))
    world.add(c, _item("c1", _day(1)), _item("c2", _day(2)))
    world.keep("c2")
    tech = world.feeds.folder_by_name("Tech")
    assert tech is not None
    assert world.service.mark_all_read(a, None, NOW) == [world.id_of("a1")]
    assert sorted(world.service.mark_all_read(None, tech.id, NOW)) == [world.id_of("b1")]
    assert sorted(world.service.mark_all_read(None, None, NOW)) == sorted(
        [world.id_of("c1"), world.id_of("c2")]
    )
    assert world.guids("read = 0") == set()
    assert world.guids("kept_page_id IS NOT NULL") == {"c2"}  # Keep untouched


def test_mark_all_read_leaves_posts_seen_after_as_of(world: World) -> None:
    feed = world.feed("https://a.example/feed")
    world.add(feed, _item("old", _day(1)))
    world.add(feed, _item("new", None), seen=NOW + timedelta(minutes=1))
    assert world.service.mark_all_read(None, None, NOW) == [world.id_of("old")]
    assert world.guids("read = 0") == {"new"}


def test_set_read_chunks_beyond_the_variable_limit(world: World) -> None:
    feed = world.feed("https://a.example/feed")
    world.add(feed, *(_item(f"p{i}", _day(1)) for i in range(33_000)))
    ids = world.service.mark_all_read(None, None, NOW)
    assert len(ids) == 33_000
    assert world.service.set_read(ids, False) == 33_000
    assert len(world.guids("read = 0")) == 33_000
```

API test, `test_mark_read_routes_mark_and_undo(env: Env)`: seed with `env.seed()`. `POST {API}/posts/mark-read` with `{"feed": ids["feed"], "as_of": NOW.isoformat()}` → 200 and `marked == len(ids_json)`. Then `POST {API}/posts/read-state` with `{"ids": <those ids>, "read": false}` → `{"marked": n}`, and `/counts` views unread is back where it started. `as_of` missing → 422; `"yesterday"` → 400 `InvalidTimestamp`; 100 001 ids → 422.

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd server && uv run pytest tests/apps/rss/test_posts.py -k "mark_all_read or chunks or mark_read_routes" -v`
Expected: FAIL (`AttributeError: 'PostService' object has no attribute 'mark_all_read'` / 404).

- [ ] **Step 3: Implement** the store method (an `UPDATE … RETURNING id` or a `SELECT` then an update in one transaction), the chunked `set_read`, the protocol entries, `PostService.mark_all_read`, and the two routes, which use `parse_timestamp`, `idem.run` and `_respond` like `read_above`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `cd server && uv run pytest tests/apps/rss -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add server/jesinia/apps/rss server/tests/apps/rss
git commit -m "feat(rss): mark a feed, folder or everything read, with bulk undo"
```

---

### Task 2: Server — feed health fields, newest post, retention override

**Files:**
- Create: `server/jesinia/apps/rss/migrations/0005_feed_retention_and_newest.sql`
- Modify: `server/jesinia/apps/rss/domain/feed.py` (`FeedView`), `domain/settings.py` (retention type + validation)
- Modify: `server/jesinia/apps/rss/infrastructure/store.py` (`SqliteFeedStore.add_posts`, `feed_views`, new `set_feed_retention`)
- Modify: `server/jesinia/apps/rss/application/ports.py` (`FeedStore`), `application/feeds.py` (`FeedService.set_retention`)
- Modify: `server/jesinia/apps/rss/api.py` (`FeedOut`, new route)
- Test: `server/tests/apps/rss/test_feeds_api.py`

**Interfaces:**
- Produces:
  - Migration: `ALTER TABLE feeds ADD COLUMN newest_post_at TEXT; ADD COLUMN retention_unit TEXT CHECK (retention_unit IN ('days','posts')); ADD COLUMN retention_value INTEGER;` plus a backfill: `UPDATE feeds SET newest_post_at = (SELECT MAX(COALESCE(published_at, first_seen_at)) FROM posts WHERE feed_id = feeds.id)`.
  - `domain/settings.py`: `RetentionUnit = Literal["days", "posts"]`; `@dataclass(frozen=True) class Retention: unit: RetentionUnit; value: int`; `RETENTION_POSTS_MIN = 10`, `RETENTION_POSTS_MAX = 10_000`; `validate_retention(unit: str, value: int) -> Retention` raising `InvalidSettings` with "retention must be 1 to 365 days" / "retention must be 10 to 10000 posts".
  - `FeedView` gains `last_ok_at: datetime | None`, `fail_count: int`, `last_error: str | None`, `newest_post_at: datetime | None`, `retention: Retention | None`.
  - `SqliteFeedStore.add_posts` sets `newest_post_at = MAX(existing, newest inserted COALESCE(published_at, first_seen_at))` in the same transaction.
  - `SqliteFeedStore.set_feed_retention(feed_id: int, retention: Retention | None) -> bool` (False if the feed is unknown or deleted).
  - `FeedService.set_retention(feed_id: int, retention: Retention | None) -> FeedView` (raises the existing feed-not-found error).
  - `FeedOut` gains `last_ok_at: str | None`, `fail_count: int`, `last_error: str | None`, `newest_post_at: str | None`, `retention: RetentionOut | None` where `RetentionOut {unit: Literal["days","posts"], value: int}`; `warning` stays.
  - `PUT /api/rss/feeds/{feed_id}/retention`, body `RetentionIn {retention: RetentionOut | None}` → `FeedOut`; 400 `InvalidSettings` when out of range.

- [ ] **Step 1: Write failing tests** in `test_feeds_api.py`:
  - `test_feed_out_carries_sync_health_and_newest_post`: add a feed via `_add` (fixture serving items), then `GET {API}/feeds` row has `last_ok_at` not null, `fail_count == 0`, `last_error is None`, `newest_post_at` equal to the newest item date in the fixture, `retention is None`.
  - `test_put_retention_sets_and_clears_the_override`: PUT `{"retention": {"unit": "posts", "value": 200}}` → 200 and the body's `retention == {"unit": "posts", "value": 200}`. PUT `{"retention": null}` → `retention is None`. `{"unit": "days", "value": 0}` → 400 `InvalidSettings`. `{"unit": "posts", "value": 5}` → 400. Unknown feed → 404.
  - `test_newest_post_survives_pruning` (in `test_posts.py`, `world` fixture): add a post at `_day(40)`, prune with the 30-day window, then `world.feeds.feed_views()[0].newest_post_at == _day(40)`.

- [ ] **Step 2: Run and confirm they fail.** `cd server && uv run pytest tests/apps/rss -k "health or retention or newest" -v`. Expected: FAIL (missing fields / 405).

- [ ] **Step 3: Implement** the migration, domain types, store changes, service method, `FeedOut.of`, and the route (idempotent, `_respond`).

- [ ] **Step 4: Run** `cd server && uv run pytest tests/apps/rss -q`. Expected: all pass.

- [ ] **Step 5: Commit** `git commit -m "feat(rss): feed sync health, newest post and per-feed retention override"`

---

### Task 3: Server — per-feed prune

**Files:**
- Modify: `server/jesinia/apps/rss/infrastructure/store.py` (`SqlitePostStore.prune`)
- Modify: `server/jesinia/apps/rss/application/posts.py` (`PostService.prune` unchanged signature)
- Test: `server/tests/apps/rss/test_posts.py`

**Interfaces:**
- Consumes: `feeds.retention_unit`, `feeds.retention_value` (Task 2).
- Produces: `SqlitePostStore.prune(default_cutoff: datetime, now: datetime) -> int` with the same signature and new semantics:
  - feeds with no override: delete unkept, non-archiving posts older than `default_cutoff` (as today);
  - `days` override: the same rule with cutoff `now - value days`;
  - `posts` override: delete unkept, non-archiving, **read** posts whose rank in the feed (newest first by `COALESCE(published_at, first_seen_at) DESC, id DESC`, counting every post of the feed) is greater than `value`; no age limit;
  - every deleted post gets a tombstone as today, and the tombstone-TTL cleanup is unchanged.
  Use a window function (`ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY …)`) for the cap.

- [ ] **Step 1: Write the failing tests:**

```python
def test_days_override_beats_the_default(world: World) -> None:
    long = world.feed("https://fried.example/feed")
    short = world.feed("https://hn.example/feed")
    world.add(long, _item("l40", _day(40)))
    world.add(short, _item("s5", _day(5)), _item("s1", _day(1)))
    world.feeds.set_feed_retention(long, Retention("days", 365))
    world.feeds.set_feed_retention(short, Retention("days", 3))
    assert world.service.prune(NOW) == 1
    assert world.guids() == {"l40", "s1"}


def test_posts_cap_prunes_only_read_posts_beyond_n(world: World) -> None:
    feed = world.feed("https://hn.example/feed")
    world.add(feed, *(_item(f"p{i}", _day(i)) for i in range(1, 16)))  # p1 newest
    world.feeds.set_feed_retention(feed, Retention("posts", 10))
    for i in range(1, 16):
        if i != 13:
            world.read(f"p{i}")
    world.keep("p14")
    assert world.service.prune(NOW) == 3  # p11, p12, p15
    assert world.guids() == {f"p{i}" for i in range(1, 11)} | {"p13", "p14"}


def test_posts_cap_never_prunes_unread(world: World) -> None:
    feed = world.feed("https://hn.example/feed")
    world.add(feed, *(_item(f"p{i}", _day(i)) for i in range(1, 51)))
    world.feeds.set_feed_retention(feed, Retention("posts", 10))
    assert world.service.prune(NOW) == 0
```

Also re-run the existing `test_prune_keeps_kept_and_prunes_undated_by_first_seen` and `test_prune_forgets_tombstones_of_items_the_feed_no_longer_offers`: they must pass unchanged, which pins the default-feed behaviour.

- [ ] **Step 2: Run them and confirm they fail.** `cd server && uv run pytest tests/apps/rss/test_posts.py -k prune -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the per-feed prune.
- [ ] **Step 4: Run** `cd server && uv run pytest tests/apps/rss -q && make -C .. coverage-check`. Expected: pass, with coverage ≥ 95% per file.
- [ ] **Step 5: Regenerate the client and commit.** `make gen-client`, then `git commit -m "feat(rss): prune each feed by its own retention; a posts cap keeps unread"`.

---

### Task 4: Design — Toast with a timed action and a close callback

**Files:**
- Modify: `web/packages/design/src/components/Toast.tsx`
- Test: `web/packages/design/src/__tests__/components.test.tsx`

**Interfaces:**
- Produces: `ToastOptions` gains `onClose?: (reason: "action" | "dismiss" | "timeout") => void`. An explicit numeric `duration` is now honoured even when `action` is set (`lifetime` checks `typeof t.duration === "number"` before the action rule). Pressing the action calls `action.onAction()`, dismisses the toast, and calls `onClose("action")`. The × button calls `onClose("dismiss")`. Expiry calls `onClose("timeout")`. `onClose` fires exactly once per toast.

- [ ] **Step 1: Write failing tests:** `action toast with a duration expires and reports timeout` (fake timers: `show({message, action, duration: 8000, onClose})`, advance 8000 ms, `onClose` called once with `"timeout"`, toast gone). `action press reports action and closes` (`onAction` and `onClose("action")` each called once). `dismiss reports dismiss`.
- [ ] **Step 2: Run** `pnpm -C web --filter @jesinia/design test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it again.** Expected: PASS, and existing toast tests are unchanged.
- [ ] **Step 5: Commit** `git commit -m "feat(design): toast actions can time out and report how they closed"`

---

### Task 5: Web API helpers

**Files:**
- Modify: `web/apps/rss/src/api.ts`
- Test: `web/apps/rss/src/__tests__/api.test.ts`

**Interfaces:**
- Consumes: the regenerated client (Tasks 1–3).
- Produces (all use `withIdempotency()` and `unwrap` like their neighbours):
  - `markAllRead(client, target: { feed: number | null; folder: number | null }, asOf: string): Promise<number[]>` (the ids).
  - `setReadMany(client, ids: number[], read: boolean): Promise<number>`
  - `setRetention(client, feedId: number, retention: Retention | null): Promise<Feed>`, with `export type Retention = { unit: "days" | "posts"; value: number }`
  - `importOpml(client, opml: string): Promise<ImportResult>`, with `ImportResult = components["schemas"]["ImportResultOut"]`
  - `getRssSettings(client)` / `putRssSettings(client, s: RssSettings)`, with `RssSettings = components["schemas"]["SettingsBody"]`
  - `OPML_EXPORT_URL = "/api/rss/opml"`
  - Delete `needsFullArticle` and `SHORT_CONTENT_CHARS` (Task 7 drops their only use).

- [ ] **Step 1: Write failing tests** in `api.test.ts` that assert each helper's method, path and body against the mocked client, matching the existing style in that file.
- [ ] **Step 2: Run** `pnpm -C web --filter @jesinia/rss test -- api`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it again.** Expected: PASS. `pnpm -C web --filter @jesinia/rss typecheck` should be clean, except for `needsFullArticle` callers, which Task 7 fixes. If you'd rather keep typecheck green, delete the function in Task 7 instead.
- [ ] **Step 5: Commit** `git commit -m "feat(rss-web): client helpers for bulk read, retention and OPML"`

---

### Task 6: Reading view — Mark all read, Refresh in the list header, sidebar hover, no errors

**Files:**
- Modify: `web/apps/rss/src/App.tsx` (`App` title-bar controls, `Workspace` list header, keys, undo)
- Modify: `web/apps/rss/src/Feeds.tsx` (drop `FeedProblem` and the `!` marker; per-row Mark read; "⚙ Manage feeds" with failing count)
- Modify: `web/apps/rss/src/keys.ts` (`KEY_HELP` gains `{ keys: ["Shift+M"], label: "Mark all read here" }`)
- Modify: `web/apps/rss/src/rss.module.css`
- Test: `web/apps/rss/src/__tests__/app.test.tsx`, `refresh.test.tsx`, `keys.test.tsx`

**Interfaces:**
- Consumes: `markAllRead`, `setReadMany` (Task 5), and Toast `duration` + `onClose` (Task 4).
- Produces:
  - `Feeds` props: remove `onFeedChanged`'s use for warnings; add `onMarkRead(target: { feed: number | null; folder: number | null }, label: string): void` and `onManage(): void` (replaces `onAddFeed`).
  - In `Workspace`, `markRead(target, label, asOf)`: optimistically sets read flags for rows in scope, calls `markAllRead`, reloads counts, and reloads the list when the target covers the current scope (`target` is everything, or `target.folder === scope.folder`, or `target.feed === scope.feed`, or the scope's feed lies in `target.folder`). It shows `toast.show({ tone: "success", message: \`Marked ${n} in ${label} read\`, action: { label: "Undo", onAction: () => setReadMany(ids, false).then(reloadAll) }, duration: 8000 })`. With n = 0 it shows "Nothing unread in {label}" and no Undo.
  - Middle-bar `asOf` is the existing list `asOf`. Sidebar `asOf` is a new `countsAsOf` state, set to the request start time whenever counts load.
  - The ↻ Refresh button and "Updated …" text move into `<header className={s.paneHead}>` beside "✓ Mark all read". The `refreshRef`/`onRefreshing`/`onRefreshed` plumbing and the title-bar `controls` are deleted.
  - Mark all read is hidden when `scope.view === "kept"` or a search is active. `Shift+M` does the same thing as the button.

- [ ] **Step 1: Write failing tests** (app.test.tsx, using its existing fixtures):
  - `mark all read in the header applies to the folder in view and offers undo`: select a folder; click "✓ Mark all read"; expect `POST /api/rss/posts/mark-read` with `{feed: null, folder: <id>}`; the toast reads "Marked 2 in Tech read"; clicking Undo posts `/posts/read-state` with those ids and `read: false`.
  - `mark all read at All unread marks everything`: body has `feed: null, folder: null`.
  - `sidebar mark read on a feed does not change the scope`: hover a feed row, click its "Mark read" button (role button, name "Mark {title} read"); the scope stays the same; counts reload.
  - `sidebar mark read on the folder in view reloads the list` (Review Focus 3): viewing feed A in folder Tech, sidebar Mark read on Tech → `listPosts` is called again and the A rows show as read.
  - `no mark all read in In KB or while searching`.
  - `Shift+M marks all read here`.
  - `the sidebar shows no feed warnings or retry`: a feed with `warning` set renders no "!" and no "Retry"; "⚙ Manage feeds · 1" is shown.
  - refresh.test.tsx: the ↻ button is inside the list pane header (`within(screen.getByRole("region", {name: …}))`), and nothing in the title bar says Refresh.
- [ ] **Step 2: Run** `pnpm -C web --filter @jesinia/rss test`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** The sidebar control is a `<button>` in each row, shown on `:hover` and `:focus-within` (CSS), with the aria-label above, and it stops propagation so the row's own click doesn't fire. Leave `FeedProblem`'s export in `ManageFeeds.tsx` for now; Task 8 rewrites that file.
- [ ] **Step 4: Run it again, plus typecheck.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(rss-web): mark all read at any level, refresh in the list, no errors in the sidebar"`

---

### Task 7: Reader — automatic full article

**Files:**
- Modify: `web/apps/rss/src/Reader.tsx`
- Test: `web/apps/rss/src/__tests__/app.test.tsx` (or a new `reader.test.tsx`)

**Interfaces:**
- Consumes: `fullArticle` (existing).
- Produces: `Reader` calls `fullArticle(client, post.id)` once on mount (`useEffect` with a `cancelled` flag set in the cleanup; a result after cleanup is dropped). `Article` state: `{ state: "loading" | "done" | "failed"; markdown: string }`. While it's loading or failed, the feed text shows. Once done, the extracted Markdown shows, along with a line "Showing full article · Show feed text". "Show feed text" switches to the feed text, and the line then reads "Showing feed text · Show full article". A failure shows nothing extra. The "Load full article" button and its error copy are removed. Keep and Engage behaviour is unchanged.

- [ ] **Step 1: Write failing tests:**
  - `loads the full article without a click`: opening a post calls `GET /api/rss/posts/{id}/article` and renders its markdown.
  - `falls back quietly`: a 502 shows the feed text and no text matching `/couldn't load/i`.
  - `toggles between full article and feed text`.
  - `ignores an article that arrives after moving on` (Review Focus 2): the article request for post 1 resolves after `j` selects post 2. Post 2's reader never shows post 1's markdown, and there's no React act/unmounted warning (spy on `console.error`).
  - `fires for a post whose feed text is long`: a post with `is_summary: false` and long content still requests the article.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm -C web --filter @jesinia/rss test && pnpm -C web --filter @jesinia/rss typecheck`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(rss-web): load the full article automatically, fall back quietly"`

---

### Task 8: Feed management screen at `/rss/manage`

**Files:**
- Create: `web/apps/rss/src/route.ts`, the `useManageRoute()` hook
- Rewrite: `web/apps/rss/src/ManageFeeds.tsx`, the screen shell: header, add form, filter, table
- Create: `web/apps/rss/src/FeedRow.tsx` (one row: folder picker, health, retention, Sync now, Remove)
- Create: `web/apps/rss/src/RetentionPicker.tsx`
- Modify: `web/apps/rss/src/App.tsx` (renders `ManageFeeds` in place of the list + reader panes when the route is manage; drops the `managing` modal state)
- Modify: `web/apps/rss/src/rss.module.css`
- Rewrite test: `web/apps/rss/src/__tests__/manage-feeds.test.tsx`

**Interfaces:**
- Consumes: `addFeed`, `moveFeed`, `renameFolder`, `refreshFeed`, `refreshFeeds`, `removeFeed`, `setRetention`, `importOpml`, `OPML_EXPORT_URL`, `getRssSettings`/`putRssSettings` (Task 5); Toast `onClose` (Task 4).
- Produces:
  - `useManageRoute(): { managing: boolean; open(): void; close(): void }`. "Managing" means `location.pathname` ends with `/manage` (served by the static fallback in `server/jesinia/core/static.py`). `open` and `close` use `history.pushState`, and a `popstate` listener keeps the hook in sync so browser Back works.
  - `ManageFeeds` props: `{ feeds: Feed[]; onChanged(): void; onRemoved(id: number): void; onBack(): void }`.
  - Header: "‹ Back to reading"; the add form (URL `Input`, folder `Input` with the existing `FOLDER_NAMES_ID` datalist, "+ Add feed"); "Sync all" (`refreshFeeds`); "Import OPML" (a file input, 5 MB check as in launcher `Rss.tsx`, then `importOpml`, with a toast summarising added/skipped/failed); "Export OPML" (`<Button href={OPML_EXPORT_URL} download="jesinia-feeds.opml">`); "Keep posts for {n} days" (all feeds), which reads and writes the global setting through `getRssSettings`/`putRssSettings`, sending `source_folder` back unchanged; and the filter "All {n} · Failing {m}", where failing means `warning !== null`.
  - Rows are sorted failing first, then by title (case-insensitive). Columns: Feed (title, then URL muted), Folder (picker; choosing "Rename folder…" prompts inline and calls `renameFolder`), Last synced (`relativeTime(last_ok_at)`, or "Never"; absolute in `title`), Newest post (`relativeTime(newest_post_at)`, or "—"), Status ("OK" / "Syncing" while this row's or Sync all's request is in flight / "Failing ({fail_count} tries)" with `last_error` beneath), Keep posts (`RetentionPicker`), and the actions "Sync now" and "Remove".
  - `RetentionPicker` props: `{ value: Retention | null; onChange(r: Retention | null): void }`. The options are Default, 7/30/90/365 days, the newest 50/200/1000 posts, and Custom… (a number input plus a unit; the server validates).
  - Remove hides the row at once and shows `toast.show({ message: \`Removed ${title}\`, action: { label: "Undo", … }, duration: 8000, onClose })`. `removeFeed` is called only when `onClose` reports `"timeout"` or `"dismiss"`. With `"action"`, the row comes back. Leaving the screen sends any pending removes first.
  - Narrow layout: each row stacks (CSS grid areas), with no horizontal scroll.
  - Delete the old modal-only code: focus trap, `FeedProblem`, `FOCUSABLE`.

- [ ] **Step 1: Write failing tests** in `manage-feeds.test.tsx`:
  - `opens at /rss/manage from the sidebar and Back returns`: clicking "⚙ Manage feeds" changes the pathname to end in `/manage` and shows the screen; "‹ Back to reading" and a `popstate` both return to the reader.
  - `adds a feed into a folder`
  - `moves a feed by changing its folder`
  - `sync now shows Syncing then the fresh status`
  - `failing feeds sort first and the Failing filter shows only them`: renders "Failing (6 tries)" and the cause.
  - `sets and clears a feed's retention`: choosing "Newest 200 posts" → `PUT …/retention {retention: {unit: "posts", value: 200}}`, then "Default" → `{retention: null}`.
  - `changes the default retention`: `PUT /api/rss/settings` with the new days and the unchanged `source_folder`.
  - `imports and exports OPML`: the export link has `href="/api/rss/opml"` and the `download` attribute; the import posts the file text.
  - `remove waits for the toast and undo keeps the feed` (Review Focus 5): Remove, then Undo, means no DELETE and the row is back. Remove, then letting the toast time out, means a DELETE. Removing the feed in view, then going back, lands on All unread.
  - `shows newest post and last synced`: "3 weeks ago", and "Never" for a null `last_ok_at`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm -C web --filter @jesinia/rss test && pnpm -C web --filter @jesinia/rss typecheck && make lint`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(rss-web): full-screen feed management with sync health, retention and OPML"`

---

### Task 9: End-to-end path and spec bookkeeping

**Files:**
- Modify: `web/tests/e2e/rss.spec.ts`
- Modify: `docs/specs/2026-10-21-rss-feed-management-design.md` (Status → Implemented, with an acceptance line)
- Modify: `docs/specs/2026-10-16-rss-reader-design.md` (a one-line pointer at §2.1, §4.1, §4.4 and §7 saying they're amended by the 2026-10-21 spec)

- [ ] **Step 1: Write the e2e test** `done for the day`: with two fixture feeds in a folder, hover a feed and click its Mark read, and the feed's count disappears. Select All unread and click "✓ Mark all read", and the count is 0 with the list empty. Click Undo, and the counts come back. Then open "⚙ Manage feeds" and see both feeds with "OK" and a Newest post value. Click "‹ Back to reading".
- [ ] **Step 2: Run** `make e2e` (or the repo's e2e target). Expected: PASS.
- [ ] **Step 3: Run the full CI suite** with `make ci`. Expected: everything passes.
- [ ] **Step 4: Update both spec docs** as listed above.
- [ ] **Step 5: Commit** `git commit -m "test(rss): done-for-the-day e2e; mark the feed management spec implemented"`
