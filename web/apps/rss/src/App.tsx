// SPDX-License-Identifier: AGPL-3.0-or-later
// The RSS reader (spec §2): feeds, list and reader in three panes; on a narrow screen a
// drill-down from feeds to list to a full-screen reader.
import type { JesiniaClient } from "@jesinia/client";
import { Button, Input, relativeTime, useToast, VisuallyHidden } from "@jesinia/design";
import { AppShell, useClient } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import {
  capCount,
  getCounts,
  listFeeds,
  listPosts,
  markReadAbove,
  refreshFeeds,
  safeHttpUrl,
  searchPosts,
  setRead,
  type Counts,
  type Feed,
  type Post,
  type Scope,
  type View,
} from "./api";
import { Feeds } from "./Feeds";
import { KEY_HELP, useKeys } from "./keys";
import { PostList } from "./PostList";
import { Reader, sentence } from "./Reader";
import s from "./rss.module.css";

export const NARROW_QUERY = "(max-width: 767px)";
export const SEARCH_DEBOUNCE_MS = 200;
const UNREAD: Scope = { view: "unread", feed: null, folder: null };

function useNarrow(): boolean {
  const query = () => (typeof window.matchMedia === "function" ? window.matchMedia(NARROW_QUERY) : null);
  const [narrow, setNarrow] = useState(() => query()?.matches ?? false);
  useEffect(() => {
    const mql = query();
    if (!mql) return;
    const onChange = () => setNarrow(mql.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);
  return narrow;
}

function causeOf(e: unknown): string {
  return sentence(e instanceof Error ? e.message : String(e));
}

function titleOf(scope: Scope, feeds: Feed[], query: string): string {
  if (query) return `Results for “${query}”`;
  if (scope.feed !== null) return feeds.find((f) => f.id === scope.feed)?.title ?? "Feed";
  if (scope.folder !== null) return feeds.find((f) => f.folder_id === scope.folder)?.folder ?? "Folder";
  return scope.view === "kept" ? "In KB" : scope.view === "all" ? "All posts" : "All unread";
}

/** Unread posts in the scope; In KB counts its kept posts. */
function headerCount(scope: Scope, counts: Counts | null): number {
  if (!counts) return 0;
  const of = (list: { id: number; unread: number }[], id: number) => list.find((c) => c.id === id)?.unread ?? 0;
  if (scope.feed !== null) return of(counts.feeds, scope.feed);
  if (scope.folder !== null) return of(counts.folders, scope.folder);
  return scope.view === "kept" ? counts.views.kept : counts.views.unread;
}

function emptyText(scope: Scope, query: string): string {
  if (query) return `No headlines match “${query}”.`;
  if (scope.view === "kept") return "Nothing kept yet. Keep a post to find it here.";
  return scope.view === "unread" ? "Nothing unread here." : "No posts here yet.";
}

function KeysDialog({ onClose }: { onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => close.current?.focus(), []);
  return (
    <div className={s.scrim} onClick={onClose}>
      <div
        className={s.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="rss-keys-title"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
      >
        <h2 id="rss-keys-title">Keyboard shortcuts</h2>
        <dl className={s.keyList}>
          {KEY_HELP.map((h) => (
            <div key={h.label}>
              <dt>
                {h.keys.map((k) => (
                  <kbd key={k}>{k}</kbd>
                ))}
              </dt>
              <dd>{h.label}</dd>
            </div>
          ))}
        </dl>
        <p className={s.muted}>Keys are off while you type in a field.</p>
        <Button ref={close} variant="secondary" size="sm" onClick={onClose}>
          Close
        </Button>
      </div>
    </div>
  );
}

/** The title bar's Refresh lives outside the workspace; they talk through these. */
type WorkspaceProps = {
  refreshRef: MutableRefObject<(() => void) | null>;
  onRefreshing: (busy: boolean) => void;
  onRefreshed: (at: string) => void;
};

function Workspace({ refreshRef, onRefreshing, onRefreshed }: WorkspaceProps) {
  const client = useClient();
  const toast = useToast();
  const narrow = useNarrow();
  const [stage, setStage] = useState<"feeds" | "list" | "reader">("list");
  const [scope, setScope] = useState<Scope>(UNREAD);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [posts, setPosts] = useState<Post[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [asOf, setAsOf] = useState(() => new Date().toISOString());
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [showKeys, setShowKeys] = useState(false);
  const [focusSearch, setFocusSearch] = useState(0);
  const searchBox = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const loadingMore = useRef(false);

  const fail = useCallback(
    (what: string, e: unknown) => toast.show({ tone: "danger", message: `Couldn't ${what}: ${causeOf(e)}` }),
    [toast],
  );
  const loadCounts = useCallback(() => {
    getCounts(client).then(setCounts, () => undefined); // stale counts are harmless; the list shows errors
  }, [client]);
  const loadFeeds = useCallback(() => {
    listFeeds(client).then(setFeeds, (e: unknown) => fail("load feeds", e));
  }, [client, fail]);

  useEffect(() => {
    loadFeeds();
    loadCounts();
  }, [loadFeeds, loadCounts]);

  useEffect(() => {
    const t = setTimeout(() => setSearch(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    const mine = ++generation.current;
    loadingMore.current = false;
    setLoading(true);
    setListError(null);
    // Read-above covers only what was on screen: posts first seen after this stay unread.
    const loadedAt = new Date().toISOString();
    const page = search
      ? searchPosts(client, search).then((items) => ({ items, next_before: null }))
      : listPosts(client, scope);
    page.then(
      (p) => {
        if (mine !== generation.current) return;
        setPosts(p.items);
        setNextBefore(p.next_before);
        setAsOf(loadedAt);
        setLoading(false);
      },
      (e: unknown) => {
        if (mine !== generation.current) return;
        setPosts([]);
        setNextBefore(null);
        setListError(causeOf(e));
        setLoading(false);
      },
    );
  }, [client, scope, search, reload]);

  const loadMore = useCallback(() => {
    if (nextBefore === null || loadingMore.current) return;
    loadingMore.current = true;
    const mine = generation.current;
    listPosts(client, scope, nextBefore).then(
      (p) => {
        if (mine !== generation.current) return;
        loadingMore.current = false;
        setPosts((prev) => {
          const seen = new Set(prev.map((x) => x.id));
          return [...prev, ...p.items.filter((x) => !seen.has(x.id))];
        });
        setNextBefore(p.next_before);
      },
      (e: unknown) => {
        if (mine !== generation.current) return;
        // Stop paging; the error says why and a reload starts over.
        setNextBefore(null);
        loadingMore.current = false;
        fail("load more posts", e);
      },
    );
  }, [client, scope, nextBefore, fail]);

  const refresh = useCallback(async () => {
    onRefreshing(true);
    try {
      await refreshFeeds(client);
      loadFeeds();
      loadCounts();
      setReload((n) => n + 1);
      onRefreshed(new Date().toISOString());
    } catch (e) {
      fail("refresh", e);
    } finally {
      onRefreshing(false);
    }
  }, [client, loadFeeds, loadCounts, onRefreshing, onRefreshed, fail]);
  useEffect(() => {
    refreshRef.current = () => void refresh();
  }, [refresh, refreshRef]);

  useEffect(() => {
    if (focusSearch) searchBox.current?.focus();
  }, [focusSearch]);

  const replace = (next: Post) => setPosts((prev) => prev.map((p) => (p.id === next.id ? next : p)));

  const markRead = (post: Post, read: boolean) => {
    replace({ ...post, read });
    setRead(client, post.id, read).then(
      (saved) => {
        replace(saved);
        loadCounts();
      },
      (e: unknown) => {
        replace(post);
        fail(`mark the post ${read ? "read" : "unread"}`, e);
      },
    );
  };

  const index = posts.findIndex((p) => p.id === selectedId);
  const selected = index >= 0 ? posts[index] : undefined;

  const select = (id: number) => {
    setSelectedId(id);
    setStage("reader");
    const post = posts.find((p) => p.id === id);
    if (post && !post.read) markRead(post, true); // spec §2.3: selecting a post reads it
  };
  const move = (delta: 1 | -1) => {
    const next = index < 0 ? (delta === 1 ? 0 : -1) : Math.min(posts.length - 1, Math.max(0, index + delta));
    const post = posts[next];
    if (post && next !== index) select(post.id);
  };
  const openOriginal = () => {
    const url = selected ? safeHttpUrl(selected.url) : null;
    if (url) window.open(url, "_blank", "noopener,noreferrer");
  };
  const readAbove = () => {
    if (!selected || search) return;
    const before = posts;
    setPosts(posts.map((p, i) => (i <= index ? { ...p, read: true } : p)));
    markReadAbove(client, selected.id, scope, asOf).then(loadCounts, (e: unknown) => {
      setPosts(before);
      fail("mark these read", e);
    });
  };
  const goTo = (next: Scope) => {
    setScope(next);
    setQuery("");
    setSearch("");
    setSelectedId(null);
    setStage("list");
  };

  // While the key list is open, keys belong to it (Esc closes it).
  useKeys(showKeys ? {} : {
    j: () => move(1),
    k: () => move(-1),
    Enter: openOriginal,
    o: openOriginal,
    m: () => selected && markRead(selected, !selected.read),
    "Shift+A": readAbove,
    "g a": () => goTo(UNREAD),
    "g i": () => goTo({ view: "kept", feed: null, folder: null }),
    "/": () => {
      setStage("list");
      setFocusSearch((n) => n + 1);
    },
    "?": () => setShowKeys(true),
  }); // prettier-ignore

  const title = titleOf(scope, feeds, search);
  const showFeeds = !narrow || stage === "feeds";
  const showList = !narrow || stage === "list";
  const showReader = !narrow || stage === "reader";

  const listPane = (
    <section className={s.listPane} aria-labelledby="rss-list-title">
      {narrow ? (
        <div className={s.subbar}>
          <Button variant="ghost" size="sm" onClick={() => setStage("feeds")}>
            ‹ Feeds
          </Button>
        </div>
      ) : null}
      <header className={s.paneHead}>
        <h2 id="rss-list-title" className={s.listTitle}>
          {title}
        </h2>
        {search ? null : <span className={s.muted}>{capCount(headerCount(scope, counts))}</span>}
        <label className={s.show}>
          <VisuallyHidden>Show</VisuallyHidden>
          <select
            value={scope.view === "all" ? "all" : "unread"}
            disabled={scope.view === "kept" || Boolean(search)}
            onChange={(e) => setScope({ ...scope, view: e.target.value as View })}
          >
            <option value="unread">Unread</option>
            <option value="all">All</option>
          </select>
        </label>
      </header>
      <div className={s.searchRow}>
        <Input
          ref={searchBox}
          label="Search headlines"
          hideLabel
          variant="search"
          placeholder="Search headlines"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {listError ? (
        <div className={s.listError} role="alert">
          <span>Couldn't load posts: {listError}</span>
          <Button variant="secondary" size="sm" onClick={() => setReload((n) => n + 1)}>
            Try again
          </Button>
        </div>
      ) : loading && posts.length === 0 ? (
        <p className={s.empty}>Loading…</p>
      ) : (
        <PostList
          posts={posts}
          selectedId={selectedId}
          onSelect={select}
          onEndReached={loadMore}
          empty={emptyText(scope, search)}
        />
      )}
    </section>
  );

  return (
    <div className={narrow ? s.narrow : s.layout}>
      {showFeeds ? <Feeds feeds={feeds} counts={counts} scope={scope} onScope={goTo} /> : null}
      {showList ? listPane : null}
      {showReader ? (
        selected ? (
          <Reader
            key={selected.id}
            post={selected}
            hasPrevious={index > 0}
            hasNext={index < posts.length - 1}
            onPrevious={() => move(-1)}
            onNext={() => move(1)}
            onToggleRead={() => markRead(selected, !selected.read)}
            onChanged={replace}
            {...(narrow ? { back: { label: title, onBack: () => setStage("list") } } : {})}
          />
        ) : (
          <section className={s.reader} aria-label="Reader">
            <p className={s.empty}>Choose a post to read it here.</p>
          </section>
        )
      ) : null}
      {showKeys ? <KeysDialog onClose={() => setShowKeys(false)} /> : null}
    </div>
  );
}

export function App({ client }: { client?: JesiniaClient }) {
  const refreshRef = useRef<(() => void) | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const controls = (
    <>
      <Button variant="ghost" size="sm" loading={refreshing} onClick={() => refreshRef.current?.()}>
        ↻ Refresh
      </Button>
      {lastUpdate ? <span className={s.updated}>Updated {relativeTime(lastUpdate)}</span> : null}
    </>
  );
  return (
    <AppShell
      app={{ id: "rss", name: "RSS", glyph: "/rss/icons/monochrome.svg" }}
      ownSearch
      controls={controls}
      {...(client ? { client } : {})}
    >
      <Workspace refreshRef={refreshRef} onRefreshing={setRefreshing} onRefreshed={setLastUpdate} />
    </AppShell>
  );
}
