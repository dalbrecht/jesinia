// SPDX-License-Identifier: AGPL-3.0-or-later
// The Review app (spec §2): workspace tabs (the queue, then one tab per opened review) over the
// queue or the opened PR. Routes: /review/ and /review/<owner>/<repo>/<n>.
import { subscribe, type JesiniaClient } from "@jesinia/client";
import { Banner, useToast, type PaletteItem } from "@jesinia/design";
import { AppShell, useClient } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type MouseEvent, type MutableRefObject } from "react";
import {
  ActionKeys,
  confirmRepo,
  dismissPr,
  errorCause,
  getPr,
  getQueue,
  isAuthMissing,
  loadPr,
  prPath,
  prRefFromText,
  retryPr,
  reviewAnyway,
  ReviewRequestError,
  routeOf,
  splitCode,
  syncedAgo,
  type OpenResult,
  type Queue as QueueData,
  type QueueRow,
} from "./api";
import { PrView } from "./pr/PrView";
import { actionId, Queue, refOf, VERDICT } from "./Queue";
import s from "./review.module.css";
import { loadTabs, sameTab, saveTabs, type OpenTab } from "./tabs";

export const NARROW_QUERY = "(max-width: 767px)";
/** While GitHub is being read for the first time (or a sync runs), the cache is read often. */
export const SYNCING_POLL_MS = 2000;
/** Otherwise the background sync (every 2 minutes) is picked up at this pace. */
export const IDLE_POLL_MS = 60_000;

/**
 * When to read the queue again. Often only while a sync is running or the very first one is
 * due; at the idle pace while GitHub is failing (auth missing, unreachable); and backing off
 * (2 s, 4 s, 8 s… up to the idle pace) while the queue itself can't be read.
 */
export function nextPollMs(queue: QueueData | null, failedReads: number): number {
  if (failedReads > 0) return Math.min(IDLE_POLL_MS, SYNCING_POLL_MS * 2 ** (failedReads - 1));
  if (queue === null) return SYNCING_POLL_MS;
  const firstRun = queue.rows.length === 0 && queue.synced_at === null && queue.sync_error === null;
  return queue.syncing || firstRun ? SYNCING_POLL_MS : IDLE_POLL_MS;
}

type Pr = { repo: string; number: number };

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

/** "Updated 20 min ago · refreshing…" (spec §2, "Always cached"). */
/**
 * "Updated 20 min ago · refreshing…" (spec §2, "Always cached"). `synced_at` is the last
 * successful sync (a failed one keeps it), so "Not updated yet" means there never was one.
 */
export function freshness(queue: QueueData, now: Date = new Date()): string {
  const ago = queue.synced_at === null ? null : syncedAgo(queue.synced_at, now);
  const text = ago === null ? "Not updated yet" : `Updated ${ago}`;
  return queue.syncing ? `${text} · refreshing…` : text;
}

const labelOf = (row: QueueRow) => `${refOf(row)} ${row.title}`;

/** A sync failure's cause, its commands shown as code (text only, never markup). */
function Cause({ text }: { text: string }) {
  return (
    <>
      {splitCode(text).map((part, i) =>
        part.code ? <code key={i}>{part.text}</code> : <span key={i}>{part.text}</span>,
      )}
    </>
  );
}

type Search = (q: string) => PaletteItem[];
type WorkspaceProps = {
  onFreshness: (queue: QueueData | null) => void;
  searchRef: MutableRefObject<Search | null>;
};

function Workspace({ onFreshness, searchRef }: WorkspaceProps) {
  const client = useClient();
  const toast = useToast();
  const narrow = useNarrow();
  const keys = useRef(new ActionKeys());
  const [queue, setQueue] = useState<QueueData | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [reads, setReads] = useState(0);
  const [attempts, setAttempts] = useState(0);
  const [tabs, setTabs] = useState<OpenTab[]>(() => {
    const saved = loadTabs();
    const linked = routeOf(window.location.pathname);
    if (linked && !saved.some((t) => sameTab(t, linked))) {
      saved.push({ ...linked, label: `${linked.repo}#${linked.number}`, verdict: null });
    }
    return saved;
  });
  const [active, setActive] = useState<Pr | null>(() => routeOf(window.location.pathname));
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const failing = useRef(false);
  const [failedReads, setFailedReads] = useState(0);

  const fail = useCallback(
    (what: string, e: unknown) => toast.show({ tone: "danger", message: `Couldn't ${what}: ${errorCause(e)}` }),
    [toast],
  );

  const readQueue = useCallback(async () => {
    try {
      const q = await getQueue(client);
      failing.current = false;
      setFailedReads(0);
      setQueue(q);
      setQueueError(null);
      setReads((n) => n + 1);
    } catch (e) {
      // Whatever was shown stays (spec §2: never replaced by an error page); say why once.
      setQueueError(errorCause(e));
      setFailedReads((n) => n + 1);
      if (!failing.current) fail("read the queue", e);
      failing.current = true;
    } finally {
      setAttempts((n) => n + 1);
    }
  }, [client, fail]);

  useEffect(() => {
    void readQueue();
  }, [readQueue]);

  // Pre-review progress arrives live; every (re)connect reads the queue again (R29), since
  // events from while the stream was down are gone.
  const latestRead = useRef(readQueue);
  latestRead.current = readQueue;
  useEffect(
    () =>
      subscribe("/api/events?topics=review.progress", () => void latestRead.current(), {
        events: ["review.progress"],
        onOpen: () => void latestRead.current(),
      }),
    [],
  );

  // The sync itself sends no event: the cache is read again on a timer (see `nextPollMs`).
  const pollMs = nextPollMs(queue, failedReads);
  useEffect(() => {
    const t = setTimeout(() => void readQueue(), pollMs);
    return () => clearTimeout(t);
  }, [readQueue, pollMs, attempts]);

  useEffect(() => onFreshness(queue), [queue, onFreshness]);

  useEffect(() => saveTabs(tabs), [tabs]);

  // Tabs follow their rows: a new title or verdict shows on the tab (and its dot).
  useEffect(() => {
    if (!queue) return;
    setTabs((prev) => {
      let changed = false;
      const next = prev.map((t) => {
        const r = queue.rows.find((x) => sameTab(x, t));
        if (!r || (t.label === labelOf(r) && t.verdict === r.recommendation)) return t;
        changed = true;
        return { ...t, label: labelOf(r), verdict: r.recommendation };
      });
      return changed ? next : prev;
    });
  }, [queue]);

  const navigate = useCallback((to: Pr | null) => {
    const path = to ? prPath(to.repo, to.number) : "/review/";
    if (window.location.pathname !== path) window.history.pushState(null, "", path);
    setActive(to);
  }, []);

  const openTab = useCallback((tab: OpenTab) => {
    setTabs((prev) => (prev.some((t) => sameTab(t, tab)) ? prev : [...prev, tab]));
  }, []);
  /** A PR's tab, opened if need be, with the row's title and verdict. */
  const tabFor = useCallback((row: QueueRow) => {
    const tab = { repo: row.repo, number: row.number, label: labelOf(row), verdict: row.recommendation };
    setTabs((prev) =>
      prev.some((t) => sameTab(t, tab)) ? prev.map((t) => (sameTab(t, tab) ? tab : t)) : [...prev, tab],
    );
  }, []);

  // Keyboard and screen-reader users keep their place: the opened review's tab takes focus,
  // and closing a tab moves focus to its neighbour, or to the Queue tab.
  const nav = useRef<HTMLElement>(null);
  const focusTab = useRef<string | null>(null);
  const tabKey = (t: Pr | null) => (t ? `${t.repo}#${t.number}` : "queue");
  useEffect(() => {
    const key = focusTab.current;
    if (key === null) return;
    const link = nav.current?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(key)}"]`);
    if (!link) return;
    focusTab.current = null;
    link.focus();
  });

  const go = useCallback(
    (to: Pr | null) => {
      focusTab.current = tabKey(to);
      navigate(to);
    },
    [navigate],
  );

  const openRow = (row: QueueRow) => {
    openTab({ repo: row.repo, number: row.number, label: labelOf(row), verdict: row.recommendation });
    go({ repo: row.repo, number: row.number });
  };

  const openMany = (rows: QueueRow[]) => {
    for (const r of rows) openTab({ repo: r.repo, number: r.number, label: labelOf(r), verdict: r.recommendation });
    if (rows[0]) go({ repo: rows[0].repo, number: rows[0].number });
  };

  const closeTab = (tab: OpenTab) => {
    const at = tabs.findIndex((t) => sameTab(t, tab));
    const neighbour = tabs[at + 1] ?? tabs[at - 1] ?? null;
    setTabs((prev) => prev.filter((t) => !sameTab(t, tab)));
    focusTab.current = tabKey(neighbour);
    // Closing the open review shows its neighbour, like a browser tab.
    if (active && sameTab(active, tab)) navigate(neighbour);
  };

  useEffect(() => {
    const onPop = () => {
      const to = routeOf(window.location.pathname);
      if (to) openTab({ ...to, label: `${to.repo}#${to.number}`, verdict: null });
      setActive(to);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [openTab]);

  /** Runs a keyed write for `action`: a retry while its outcome is unknown reuses its key. */
  const keyed = useCallback(async <T,>(action: string, work: (key: string) => Promise<T>): Promise<T> => {
    setBusy((prev) => new Set(prev).add(action));
    try {
      const result = await work(keys.current.keyFor(action));
      keys.current.settle(action);
      return result;
    } catch (e) {
      keys.current.settle(action, e);
      throw e;
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(action);
        return next;
      });
    }
  }, []);

  const load = useCallback(
    async (ref: string) => {
      const row = await keyed(`load:${ref}`, (key) => loadPr(client, ref, key));
      tabFor(row);
      go({ repo: row.repo, number: row.number });
      void readQueue();
    },
    [client, keyed, tabFor, go, readQueue],
  );

  // Opening /review/<owner>/<repo>/<n> directly loads that PR (spec §2, "Load a PR"), unless it
  // is cached already: reloading a review's link opens it again and never re-runs Load (M4).
  const linked = useRef(routeOf(window.location.pathname));
  useEffect(() => {
    const to = linked.current;
    if (!to) return;
    const path = prPath(to.repo, to.number);
    const load = () =>
      keyed(`load:${path}`, (key) => loadPr(client, path, key)).then(
        (row) => {
          tabFor(row);
          void readQueue();
        },
        (e: unknown) => fail(`load ${to.repo}#${to.number}`, e),
      );
    getPr(client, to.repo, to.number).then(
      (view) => tabFor(view.pr),
      (e: unknown) => {
        if (e instanceof ReviewRequestError && e.code === "PrNotInQueueError") void load();
      },
    );
  }, [client, keyed, tabFor, readQueue, fail]);

  const dismiss = (row: QueueRow) => {
    keyed(actionId("dismiss", row), (key) => dismissPr(client, row.repo, row.number, key)).then(
      () => {
        setQueue((q) => (q ? { ...q, rows: q.rows.filter((r) => !sameTab(r, row)) } : q));
        void readQueue();
      },
      (e: unknown) => fail(`dismiss ${refOf(row)}`, e),
    );
  };

  const started = (row: QueueRow, what: string, result: OpenResult) => {
    toast.show({ tone: "info", message: result.state === "started" ? `${what} ${refOf(row)}.` : result.reason });
    openRow(row);
    void readQueue();
  };

  const anyway = (row: QueueRow) => {
    keyed(actionId("anyway", row), (key) => reviewAnyway(client, row.repo, row.number, key)).then(
      (r) => started(row, "Reviewing anyway:", r),
      (e: unknown) => fail(`review ${refOf(row)} anyway`, e),
    );
  };

  const retry = (row: QueueRow) => {
    keyed(actionId("retry", row), (key) => retryPr(client, row.repo, row.number, key)).then(
      (r) => started(row, "Retrying", r),
      (e: unknown) => fail(`retry ${refOf(row)}`, e),
    );
  };

  const confirm = (repo: string) => {
    keyed(`confirm:${repo}`, (key) => confirmRepo(client, repo, key)).then(
      () => {
        toast.show({ tone: "success", message: `Watching ${repo}: its PRs are reviewed automatically.` });
        void readQueue();
      },
      (e: unknown) => fail(`confirm ${repo}`, e),
    );
  };

  // ⌘K: Load a pasted PR, or open a PR in the queue by its title or number.
  searchRef.current = (q: string): PaletteItem[] => {
    const items: PaletteItem[] = [];
    const ref = prRefFromText(q);
    if (ref) {
      items.push({
        id: `load-${ref}`,
        label: `Load ${ref}`,
        detail: "Review it now",
        group: "app",
        onRun: () => void load(ref).catch((e: unknown) => fail(`load ${ref}`, e)),
      });
    }
    const needle = q.trim().toLowerCase();
    for (const r of queue?.rows ?? []) {
      if (items.length >= 7) break;
      if (!labelOf(r).toLowerCase().includes(needle)) continue;
      items.push({ id: `open-${r.repo}#${r.number}`, label: `Open ${refOf(r)}`, detail: r.title, group: "app", onRun: () => openRow(r) });
    }
    return items;
  }; // prettier-ignore

  const follow = (e: MouseEvent<HTMLAnchorElement>, to: Pr | null) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // a new tab or window
    e.preventDefault();
    navigate(to);
  };

  const syncError = queue?.sync_error ?? null;
  const activeTab = active ? (tabs.find((t) => sameTab(t, active)) ?? null) : null;

  return (
    <div className={s.workspace}>
      <nav ref={nav} className={s.wtabs} aria-label="Open reviews">
        <a
          className={s.wtab}
          data-tab="queue"
          href="/review/"
          aria-current={active === null ? "page" : undefined}
          onClick={(e) => follow(e, null)}
        >
          <span aria-hidden="true">☰</span> Queue
        </a>
        {tabs.map((t) => {
          const verdict = t.verdict ? VERDICT[t.verdict] : undefined;
          const ref = t.label.split(" ", 1).join("");
          return (
            <span key={`${t.repo}#${t.number}`} className={s.wtabItem}>
              <a
                className={s.wtab}
                data-tab={tabKey(t)}
                href={prPath(t.repo, t.number)}
                aria-current={active && sameTab(active, t) ? "page" : undefined}
                onClick={(e) => follow(e, t)}
              >
                <span
                  className={s.dot}
                  data-tone={verdict?.tone ?? "none"}
                  role="img"
                  aria-label={verdict ? verdict.text.slice(2) : "Not reviewed yet"}
                  title={verdict ? verdict.text.slice(2) : "Not reviewed yet"}
                />{" "}
                <span className={s.wtabLabel}>{t.label}</span>
              </a>
              <button type="button" className={s.wtabClose} aria-label={`Close ${ref}`} onClick={() => closeTab(t)}>
                ✕
              </button>
            </span>
          );
        })}
      </nav>
      {syncError !== null ? (
        <div className={s.banners}>
          {isAuthMissing(syncError) ? (
            <Banner tone="danger" cause="GitHub auth missing:">
              <Cause text={syncError} />
              {queue?.rows.length ? " What you see is from the cache." : null}
            </Banner>
          ) : (
            <Banner tone="danger" cause="Couldn't refresh from GitHub:">
              <Cause text={syncError} />
              {queue?.rows.length ? " What you see is from the cache; it refreshes when GitHub answers." : null}
            </Banner>
          )}
        </div>
      ) : null}
      <div className={s.body} hidden={activeTab !== null}>
        {queue ? (
          <Queue
            queue={queue}
            narrow={narrow}
            busy={busy}
            onLoad={load}
            onOpen={openRow}
            onOpenMany={openMany}
            onDismiss={dismiss}
            onReviewAnyway={anyway}
            onRetry={retry}
            onConfirmRepo={confirm}
          />
        ) : queueError ? (
          <div className={s.problem} role="alert">
            <span>{`Couldn't read the queue: ${queueError}`}</span>
          </div>
        ) : null}
      </div>
      {activeTab ? (
        <div className={s.body}>
          <PrView
            key={`${activeTab.repo}#${activeTab.number}`}
            repo={activeTab.repo}
            number={activeTab.number}
            refresh={reads}
          />
        </div>
      ) : null}
    </div>
  );
}

export function App({ client }: { client?: JesiniaClient }) {
  // Each read of the queue (every minute at the least) renders the freshness line again.
  const [queue, setQueue] = useState<QueueData | null>(null);
  const searchRef = useRef<Search | null>(null);
  const appSearch = useCallback(async (q: string) => searchRef.current?.(q) ?? [], []);
  const controls = queue ? <span className={s.updated}>{freshness(queue)}</span> : null;
  return (
    <AppShell
      app={{ id: "review", name: "Review", glyph: "/review/icons/monochrome.svg" }}
      controls={controls}
      appSearch={appSearch}
      {...(client ? { client } : {})}
    >
      <Workspace onFreshness={setQueue} searchRef={searchRef} />
    </AppShell>
  );
}
