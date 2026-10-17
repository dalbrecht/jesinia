// SPDX-License-Identifier: AGPL-3.0-or-later
// The RSS reader (spec §2): feeds, list and reader in three panes; on a narrow screen a
// drill-down from feeds to list to a full-screen reader.
import { subscribe, type JesiniaClient } from "@jesinia/client";
import { Button, Input, relativeTime, useToast, VisuallyHidden } from "@jesinia/design";
import { AppShell, ChatRail, useClient, type ChatState, type Runners } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MutableRefObject } from "react";
import {
  askTakeaway,
  capCount,
  engagePost,
  errorCause,
  getCounts,
  listFeeds,
  listPosts,
  markReadAbove,
  refreshFeeds,
  RssRequestError,
  safeHttpUrl,
  searchPosts,
  setRead,
  type Counts,
  type Feed,
  type Kept,
  type Post,
  type Scope,
  type View,
} from "./api";
import { Feeds } from "./Feeds";
import { KeepPopover } from "./KeepPopover";
import { KEY_HELP, useKeys } from "./keys";
import { ManageFeeds } from "./ManageFeeds";
import { PostList } from "./PostList";
import { Reader } from "./Reader";
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

/**
 * `items` from a reload of the same list, with the open post kept where it was if the answer
 * no longer has it (R30).
 */
export function keepOpen(prev: Post[], items: Post[], openId: number | null): Post[] {
  if (openId === null || items.some((p) => p.id === openId)) return items;
  const at = prev.findIndex((p) => p.id === openId);
  const open = prev[at];
  if (!open) return items;
  return [...items.slice(0, at), open, ...items.slice(at)];
}

const causeOf = errorCause;

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
  useEffect(() => {
    // Back to where the reader was when the list closes.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    close.current?.focus();
    return () => opener?.focus();
  }, []);
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
          } else if (e.key === "Tab") {
            e.preventDefault(); // Close is the only control: Tab stays on it (modal)
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
  /** null while the server's runners are still loading. */
  runners: Runners | null;
};

/** The Keep popover, open for one post; after Engage it carries the takeaway. */
type Keeping = {
  postId: number;
  note: string;
  takeaway: { text: string; session: string } | null;
  /** Each opening is a fresh popover, even for the same post and session. */
  openId: number;
  /** How many archive events had arrived when it opened (see `freshen`). */
  since: number;
};
/** An archive state change from the server (`rss.post_changed`, R22). */
type PostChanged = { id: number; archive_state: Post["archive_state"]; archive_error: Post["archive_error"] };
/**
 * The conversation about one post; `tried` once a session was asked for on opening. Closing
 * the rail (or Done on the sheet) only hides it: the chat keeps its state until another post
 * opens.
 */
type Engaged = { postId: number; session: string | null; tried: boolean; open: boolean };

function Workspace({ refreshRef, onRefreshing, onRefreshed, runners }: WorkspaceProps) {
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
  /** The list (scope and search) whose answer is on screen: its reload never blanks it. */
  const [loadedList, setLoadedList] = useState<string | null>(null);
  /** The open post and the rows, for answers that arrive after a render. */
  const openPost = useRef<number | null>(null);
  const shownPosts = useRef<Post[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [showKeys, setShowKeys] = useState(false);
  const [managing, setManaging] = useState(false);
  const [focusSearch, setFocusSearch] = useState(0);
  const searchBox = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const loadingMore = useRef(false);
  const shownList = useRef("");
  /** Per post, the latest read toggle: older answers arriving late are ignored. */
  const readSeq = useRef(new Map<number, number>());
  const countsFailing = useRef(false);
  const [keeping, setKeeping] = useState<Keeping | null>(null);
  const [engaged, setEngaged] = useState<Engaged | null>(null);
  const [takingAway, setTakingAway] = useState(false);
  /** The post the conversation is about: a session that starts after another post opened is let go. */
  const engagedPost = useRef<number | null>(null);
  const openings = useRef(0);
  const runner = Object.entries(runners ?? {}).find(([, r]) => r.available)?.[0] ?? null;
  const hasAgent = runners === null || runner !== null;

  const fail = useCallback(
    // §9: the cause, then what to do. A request that got no answer says so with its own fix.
    (what: string, e: unknown, next?: string) =>
      toast.show({
        tone: "danger",
        message: `Couldn't ${what}: ${causeOf(e)}${next && e instanceof RssRequestError ? ` ${next}` : ""}`,
      }),
    [toast],
  );
  const loadCounts = useCallback(() => {
    getCounts(client).then(
      (c) => {
        countsFailing.current = false;
        setCounts(c);
      },
      (e: unknown) => {
        // Counts reload after every read; say why once, not on each retry.
        if (countsFailing.current) return;
        countsFailing.current = true;
        fail("load unread counts", e, "They reload with the next ↻ Refresh.");
      },
    );
  }, [client, fail]);
  const loadFeeds = useCallback(() => {
    listFeeds(client).then(setFeeds, (e: unknown) => fail("load feeds", e, "Press ↻ Refresh to try again."));
  }, [client, fail]);

  useEffect(() => {
    loadFeeds();
    loadCounts();
  }, [loadFeeds, loadCounts]);

  // Archives finish in the background: their state arrives live (R22). A short archive can
  // finish before the answer to the Keep (or Retry) that started it arrives, so the latest
  // news per post is remembered, and an answer never takes a post back to an older state.
  const archiveEvents = useRef(0);
  const archiveNews = useRef(new Map<number, { at: number; change: PostChanged }>());
  const resync = useRef(() => {});
  resync.current = () => {
    loadCounts();
    setReload((n) => n + 1);
  };
  useEffect(
    () =>
      subscribe<PostChanged>(
        "/api/events?topics=rss.post_changed",
        ({ data }) => {
          archiveEvents.current += 1;
          archiveNews.current.set(data.id, { at: archiveEvents.current, change: data });
          setPosts((prev) =>
            prev.map((p) =>
              p.id === data.id ? { ...p, archive_state: data.archive_state, archive_error: data.archive_error } : p,
            ),
          );
        },
        // R29: events from while the stream was down (a restart, say) are gone; every
        // (re)connect refetches what they would have changed.
        { events: ["rss.post_changed"], onOpen: () => resync.current() },
      ),
    [],
  );
  /** `post` as answered, unless archive news for it arrived after `since`. */
  const freshen = useCallback((post: Post, since: number): Post => {
    const news = archiveNews.current.get(post.id);
    if (!news || news.at <= since) return post;
    return { ...post, archive_state: news.change.archive_state, archive_error: news.change.archive_error };
  }, []);
  const trackArchive = useCallback(
    async (work: () => Promise<Post>) => {
      const since = archiveEvents.current;
      return freshen(await work(), since);
    },
    [freshen],
  );

  useEffect(() => {
    const t = setTimeout(() => setSearch(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    const mine = ++generation.current;
    loadingMore.current = false;
    setLoading(true);
    setListError(null);
    // A different list: drop the old one at once, so nothing in it can be acted on under the
    // new scope (read-above, paging with its cursor). A reload of the same list keeps its rows
    // on screen until the answer replaces them (R30).
    const listKey = JSON.stringify([scope, search]);
    const sameList = listKey === shownList.current;
    if (!sameList) {
      shownList.current = listKey;
      setPosts([]);
      setNextBefore(null);
    }
    // Read-above covers only what was on screen: posts first seen after this stay unread.
    const loadedAt = new Date().toISOString();
    const page = search
      ? searchPosts(client, search).then((items) => ({ items, next_before: null }))
      : listPosts(client, scope);
    page.then(
      (p) => {
        if (mine !== generation.current) return;
        // The open post stays until the reader moves on, even if it no longer matches (read
        // in All unread): Keep's typed note and the Engage chat belong to it (R30).
        setPosts((prev) => (sameList ? keepOpen(prev, p.items, openPost.current) : p.items));
        setNextBefore(p.next_before);
        setAsOf(loadedAt);
        setLoadedList(listKey);
        setLoading(false);
      },
      (e: unknown) => {
        if (mine !== generation.current) return;
        setLoading(false);
        if (sameList && shownPosts.current.length > 0) {
          // A failed reload leaves what is on screen: say why and how to retry.
          fail("reload posts", e, "Press ↻ Refresh to try again.");
          return;
        }
        setPosts([]);
        setNextBefore(null);
        setListError(causeOf(e));
      },
    );
  }, [client, scope, search, reload, fail]);

  const loadMore = useCallback(() => {
    if (loading || nextBefore === null || loadingMore.current) return;
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
        fail("load more posts", e, "Press ↻ Refresh to start the list again.");
      },
    );
  }, [client, scope, nextBefore, loading, fail]);

  const refresh = useCallback(async () => {
    onRefreshing(true);
    try {
      await refreshFeeds(client);
      loadFeeds();
      loadCounts();
      setReload((n) => n + 1);
      onRefreshed(new Date().toISOString());
    } catch (e) {
      fail("refresh", e, "Try again in a moment.");
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

  const setReadFlags = (flags: Map<number, boolean>) =>
    setPosts((prev) => prev.map((p) => (flags.has(p.id) ? { ...p, read: flags.get(p.id) === true } : p)));

  const markRead = (post: Post, read: boolean) => {
    const seq = (readSeq.current.get(post.id) ?? 0) + 1;
    readSeq.current.set(post.id, seq);
    const latest = () => readSeq.current.get(post.id) === seq;
    setReadFlags(new Map([[post.id, read]]));
    // Like Keep and Retry: an answer never takes the post back to an older archive state.
    const since = archiveEvents.current;
    setRead(client, post.id, read).then(
      (saved) => {
        if (latest()) replace(freshen(saved, since));
        loadCounts();
      },
      (e: unknown) => {
        if (latest()) setReadFlags(new Map([[post.id, !read]]));
        fail(`mark the post ${read ? "read" : "unread"}`, e, "Press m to try again.");
      },
    );
  };

  openPost.current = selectedId;
  shownPosts.current = posts;
  const index = posts.findIndex((p) => p.id === selectedId);
  const selected = index >= 0 ? posts[index] : undefined;

  // The open post left the list (a search that doesn't match it): let it go, and on a narrow
  // screen go back to the list rather than an empty reader. A reload of the same list keeps
  // it (R30).
  useEffect(() => {
    if (loading || selectedId === null || selected) return;
    setSelectedId(null);
    setStage((st) => (st === "reader" ? "list" : st));
  }, [loading, selectedId, selected]);

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
    if (!selected || search || loading) return;
    const above = posts.slice(0, index + 1);
    setReadFlags(new Map(above.map((p) => [p.id, true])));
    markReadAbove(client, selected.id, scope, asOf).then(loadCounts, (e: unknown) => {
      // Undo only the read flags it set; rows paged in meanwhile stay.
      setReadFlags(new Map(above.map((p) => [p.id, p.read])));
      fail("mark these read", e, "Press Shift+A to try again.");
    });
  };

  // Keep and Engage belong to the post they were opened for.
  useEffect(() => {
    setKeeping((k) => (k && k.postId !== selectedId ? null : k));
    setEngaged((e) => (e && e.postId !== selectedId ? null : e));
    if (engagedPost.current !== selectedId) engagedPost.current = null;
  }, [selectedId]);

  /** The session being created for a post: a send meanwhile waits for it, never makes a second. */
  const creating = useRef<{ postId: number; session: Promise<string | null> } | null>(null);
  const startSession = useCallback(
    (postId: number, chosen: string): Promise<string | null> => {
      if (creating.current?.postId === postId) return creating.current.session;
      const create = async (): Promise<string | null> => {
        try {
          const created = await engagePost(client, postId, chosen);
          if (engagedPost.current !== postId) return null; // another post opened meanwhile
          setEngaged((e) => (e && e.postId === postId ? { ...e, session: created.id } : e));
          return created.id;
        } catch (e) {
          if (engagedPost.current === postId) fail("start the chat", e, "Send a message to try again.");
          return null;
        }
      };
      const entry = { postId, session: create() };
      creating.current = entry;
      // Settled: the session is installed, or the next message tries again.
      void entry.session.then(() => {
        if (creating.current === entry) creating.current = null;
      });
      return entry.session;
    },
    [client, fail],
  );

  // Engage starts the session as the rail opens, once a runner is known; if that fails, the
  // first message tries again.
  useEffect(() => {
    if (!engaged || engaged.session || engaged.tried || runner === null) return;
    setEngaged({ ...engaged, tried: true });
    void startSession(engaged.postId, runner);
  }, [engaged, runner, startSession]);

  const openKeep = (next: Omit<Keeping, "openId" | "since">) => {
    openings.current += 1;
    setKeeping({ ...next, openId: openings.current, since: archiveEvents.current });
    // On a narrow screen the chat is a sheet over the reader: it steps aside for the popover.
    if (narrow) setEngaged((e) => (e ? { ...e, open: false } : e));
  };
  const keep = () => {
    if (selected) openKeep({ postId: selected.id, note: "", takeaway: null });
  };
  const engage = () => {
    if (!selected) return;
    if (engaged?.postId === selected.id) {
      setEngaged({ ...engaged, open: true }); // back to the same conversation
      return;
    }
    engagedPost.current = selected.id;
    setEngaged({ postId: selected.id, session: null, tried: false, open: true });
  };
  const closeChat = () => setEngaged((e) => (e ? { ...e, open: false } : e));
  const kept = (result: Kept, since: number) => {
    replace(freshen(result.post, since));
    setKeeping(null);
    loadCounts();
    toast.show({ tone: "success", message: `Kept to ${result.path}.` });
  };
  const keepWithConversation = async (chat: ChatState, postId: number, session: string) => {
    // The author's own last message: the takeaway prompt's turn is marked and never counts (R26).
    const note = chat.lastUserText;
    setTakingAway(true);
    try {
      const takeaway = await askTakeaway(client, session);
      openKeep({ postId, note, takeaway: { text: takeaway.text, session } });
    } catch (e) {
      fail("get a takeaway", e);
    } finally {
      setTakingAway(false);
    }
  };

  const goTo = (next: Scope) => {
    setScope(next);
    setQuery("");
    setSearch("");
    setSelectedId(null);
    setStage("list");
  };

  /** Feeds or folders changed (an add, move, rename, unsubscribe or Retry). */
  const feedsChanged = () => {
    loadFeeds();
    loadCounts();
    setReload((n) => n + 1);
  };
  const added = (feed: Feed) => {
    setManaging(false);
    loadFeeds();
    loadCounts();
    goTo({ view: "unread", feed: feed.id, folder: null });
  };

  // While a dialog or the Keep popover is open, keys belong to it (Esc closes it).
  useKeys(showKeys || keeping || managing ? {} : {
    j: () => move(1),
    k: () => move(-1),
    Enter: openOriginal,
    o: openOriginal,
    n: keep,
    e: engage,
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
  // `/` searches the feed list as well as headlines (spec §2.2): feeds are all loaded, so their
  // names are matched here.
  const needle = search.toLowerCase();
  const matchingFeeds = needle ? feeds.filter((f) => f.title.toLowerCase().includes(needle)) : [];
  const chat =
    selected !== undefined && engaged !== null && engaged.postId === selected.id
      ? { post: selected, session: engaged.session, open: engaged.open }
      : null;
  const chatOpen = chat !== null && chat.open;
  // Engaged on a wide screen, the feeds give way so the reader keeps a usable measure.
  const showFeeds = narrow ? stage === "feeds" : !chatOpen;
  // Until the effect above settles, a reader stage with nothing selected is the list.
  const shown = stage === "reader" && !selected ? "list" : stage;
  const showList = !narrow || shown === "list";
  const showReader = !narrow || shown === "reader";

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
          placeholder="Search feeds and headlines"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {matchingFeeds.length ? (
        <div className={s.feedMatches} role="group" aria-label="Matching feeds">
          <h3 className={s.feedMatchesTitle}>Feeds</h3>
          {matchingFeeds.map((f) => (
            <button
              key={f.id}
              type="button"
              className={s.feed}
              onClick={() => goTo({ view: "unread", feed: f.id, folder: null })}
            >
              {f.title}
            </button>
          ))}
        </div>
      ) : null}
      {listError ? (
        <div className={s.listError} role="alert">
          <span>Couldn't load posts: {listError}</span>
          <Button variant="secondary" size="sm" onClick={() => setReload((n) => n + 1)}>
            Try again
          </Button>
        </div>
      ) : loading && posts.length === 0 && loadedList !== shownList.current ? (
        <p className={s.empty}>Loading…</p>
      ) : (
        <PostList
          key={shownList.current}
          posts={posts}
          selectedId={selectedId}
          onSelect={select}
          onStep={move}
          onEndReached={loadMore}
          empty={emptyText(scope, search)}
        />
      )}
    </section>
  );

  return (
    <div className={narrow ? s.narrow : chatOpen ? s.engagedLayout : s.layout}>
      {showFeeds ? (
        <Feeds
          feeds={feeds}
          counts={counts}
          scope={scope}
          onScope={goTo}
          onAddFeed={() => setManaging(true)}
          onFeedChanged={feedsChanged}
        />
      ) : null}
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
            trackArchive={trackArchive}
            onKeep={keep}
            onEngage={engage}
            engaged={chatOpen}
            {...(narrow ? { back: { label: title, onBack: () => setStage("list") } } : {})}
          >
            {keeping && keeping.postId === selected.id ? (
              <KeepPopover
                key={keeping.openId}
                post={selected}
                initialNote={keeping.note}
                takeaway={keeping.takeaway}
                hasAgent={hasAgent}
                onKept={(result) => kept(result, keeping.since)}
                onCancel={() => setKeeping(null)}
              />
            ) : null}
          </Reader>
        ) : (
          <section className={s.reader} aria-label="Reader">
            <p className={s.empty}>Choose a post to read it here.</p>
          </section>
        )
      ) : null}
      {chat ? (
        <EngageChat
          open={chat.open}
          narrow={narrow}
          post={chat.post}
          session={chat.session}
          runners={runners}
          onSessionNeeded={(chosen) => startSession(chat.post.id, chosen)}
          onClose={closeChat}
          takingAway={takingAway}
          onKeepWithConversation={(state, session) => void keepWithConversation(state, chat.post.id, session)}
        />
      ) : null}
      {showKeys ? <KeysDialog onClose={() => setShowKeys(false)} /> : null}
      {managing ? (
        <ManageFeeds
          feeds={feeds}
          onAdded={added}
          onChanged={feedsChanged}
          // R28: unsubscribing the feed in view goes back to All unread.
          onRemoved={(id) => {
            // R28 and M11: the feed in view, or the last feed of the folder in view, is gone.
            const folderGone = scope.folder !== null && !feeds.some((f) => f.id !== id && f.folder_id === scope.folder);
            if (scope.feed === id || folderGone) goTo(UNREAD);
          }}
          onClose={() => setManaging(false)}
        />
      ) : null}
    </div>
  );
}

type EngageChatProps = {
  /** Hidden, not unmounted, while closed: the transcript lives in the rail. */
  open: boolean;
  narrow: boolean;
  post: Post;
  session: string | null;
  runners: Runners | null;
  onSessionNeeded: (runner: string) => Promise<string | null>;
  onClose: () => void;
  takingAway: boolean;
  onKeepWithConversation: (chat: ChatState, session: string) => void;
};

/** Engage (spec §3.2): the chat rail beside the reader, or a sheet over it on a narrow screen. */
function EngageChat(props: EngageChatProps) {
  const { session, open, narrow } = props;
  const slot = useRef<HTMLDivElement>(null);
  // The sheet covers the reader: it takes focus to its message box.
  useEffect(() => {
    if (open && narrow) slot.current?.querySelector("textarea")?.focus();
  }, [open, narrow]);
  const rail = (
    <ChatRail
      className={props.narrow ? s.sheetChat : s.chatRail}
      label="Chat about this post"
      subject={`“${props.post.title}”`}
      placeholder="Ask about this post"
      session={session}
      onSessionNeeded={props.onSessionNeeded}
      runners={props.runners}
      actions={
        props.narrow ? (
          <Button variant="ghost" size="sm" onClick={props.onClose}>
            Done
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            icon="⇥"
            aria-label="Close chat"
            title="Close chat"
            onClick={props.onClose}
          />
        )
      }
      {...(session
        ? {
            footer: (chat: ChatState) => (
              <Button
                size="sm"
                variant="primary"
                disabled={!chat.lastReply || chat.busy}
                loading={props.takingAway}
                onClick={() => props.onKeepWithConversation(chat, session)}
              >
                Keep with this conversation
              </Button>
            ),
          }
        : {})}
    />
  );
  // One tree for both layouts, so a resize across the breakpoint keeps the transcript.
  return (
    <div
      ref={slot}
      className={props.narrow ? s.sheet : s.railSlot}
      hidden={!props.open}
      {...(props.narrow
        ? {
            role: "dialog",
            "aria-label": "Chat about this post",
            // Esc is Done.
            onKeyDown: (e: KeyboardEvent) => {
              if (e.key === "Escape") props.onClose();
            },
          }
        : {})}
    >
      {props.narrow ? (
        <div className={s.grab} aria-hidden="true">
          <i />
        </div>
      ) : null}
      {rail}
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
      {(status) => (
        <Workspace
          refreshRef={refreshRef}
          onRefreshing={setRefreshing}
          onRefreshed={setLastUpdate}
          runners={status.runners}
        />
      )}
    </AppShell>
  );
}
