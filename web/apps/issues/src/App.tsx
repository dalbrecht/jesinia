// SPDX-License-Identifier: AGPL-3.0-or-later
// The Issues app (Issues spec §6): workspace tabs (Inbox, Backlog, Epics and Queues, then one tab
// per opened issue or epic) over the list, the opened issue or the opened epic, with the Pending
// changes tray pinned at the bottom whenever it isn't empty. Routes: /issues/, /issues/backlog,
// /issues/epics, /issues/queues, /issues/<owner>/<repo>/<n> and /issues/epics/<owner>/<repo>/<n>.
// Compact density (ADR 0008).
import type { JesiniaClient } from "@jesinia/client";
import { useToast } from "@jesinia/design";
import { AppShell, useClient, VisibleText } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import { errorCause, getRepos, refOf, routeOf, routePath, type IssueRow, type Repo, type Route } from "./api";
import { Backlog } from "./Backlog";
import { Epics, EpicTab } from "./Epics";
import { Inbox } from "./Inbox";
import s from "./issues.module.css";
import { IssueView } from "./IssueView";
import { Queues } from "./Queues";
import { loadTabs, sameTab, saveTabs, type OpenTab } from "./tabs";
import { TrayProvider, useTray } from "./Tray";

type Fixed = "inbox" | "backlog" | "epics" | "queues";
const FIXED: { kind: Fixed; label: string }[] = [
  { kind: "inbox", label: "Inbox" },
  { kind: "backlog", label: "Backlog" },
  { kind: "epics", label: "Epics" },
  { kind: "queues", label: "Queues" },
];

type Opened = Extract<Route, { kind: "issue" | "epic" }>;

/** A screen's key: issues and epics by ref, without case (GitHub's repo names compare that way). */
const tabKey = (route: Route) =>
  route.kind === "issue"
    ? `${route.repo}#${route.number}`.toLowerCase()
    : route.kind === "epic"
      ? `epic:${route.repo}#${route.number}`.toLowerCase()
      : route.kind;
/** The screen an opened tab shows. */
const routeOfTab = (tab: OpenTab): Opened =>
  tab.kind === "epic"
    ? { kind: "epic", repo: tab.repo, number: tab.number }
    : { kind: "issue", repo: tab.repo, number: tab.number };
const issueKey = (tab: OpenTab) => tabKey(routeOfTab(tab));
const isOpened = (route: Route): route is Opened => route.kind === "issue" || route.kind === "epic";
/** The tab a linked or popped address opens (labelled by its ref until its title is known). */
const tabOf = (route: Opened): OpenTab => ({
  repo: route.repo,
  number: route.number,
  label: `${route.repo}#${route.number}`,
  ...(route.kind === "epic" ? { kind: "epic" as const } : {}),
});

function Workspace() {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const [route, setRoute] = useState<Route>(() => routeOf(window.location.pathname));
  const [tabs, setTabs] = useState<OpenTab[]>(() => {
    const saved = loadTabs();
    const linked = routeOf(window.location.pathname);
    if (isOpened(linked) && !saved.some((t) => sameTab(t, linked))) saved.push(tabOf(linked));
    return saved;
  });
  const [repos, setRepos] = useState<Repo[]>([]);
  // A list or an issue mounts the first time its tab is shown, then stays mounted (hidden while
  // another tab is shown) until its tab closes: it keeps its place, selection and scroll.
  const [visited, setVisited] = useState<ReadonlySet<string>>(
    () => new Set([tabKey(routeOf(window.location.pathname))]),
  );
  // Keyboard and screen-reader users keep their place: the opened issue's tab takes focus, and
  // closing a tab moves focus to its neighbour, or to the Inbox tab.
  const nav = useRef<HTMLElement>(null);
  const focusTab = useRef<string | null>(null);
  useEffect(() => {
    const key = focusTab.current;
    if (key === null) return;
    const link = nav.current?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(key)}"]`);
    if (!link) return;
    focusTab.current = null;
    link.focus();
  });

  useEffect(() => {
    getRepos(client).then(setRepos, (e: unknown) =>
      toast.show({ tone: "danger", message: `Couldn't read the watched repos: ${errorCause(e)}` }),
    );
  }, [client, toast, tray.revision]);

  useEffect(() => saveTabs(tabs), [tabs]);

  const show = useCallback((to: Route) => {
    setVisited((prev) => (prev.has(tabKey(to)) ? prev : new Set(prev).add(tabKey(to))));
    setRoute(to);
  }, []);

  const navigate = useCallback(
    (to: Route) => {
      const path = routePath(to);
      if (window.location.pathname !== path) window.history.pushState(null, "", path);
      show(to);
    },
    [show],
  );

  const openTab = useCallback((tab: OpenTab) => {
    setTabs((prev) => (prev.some((t) => sameTab(t, tab)) ? prev : [...prev, tab]));
  }, []);

  useEffect(() => {
    const onPop = () => {
      const to = routeOf(window.location.pathname);
      if (isOpened(to)) openTab(tabOf(to));
      show(to);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [openTab, show]);

  const openIssue = useCallback(
    (repo: string, number: number, title?: string) => {
      const label = title === undefined ? `${repo}#${number}` : `${repo}#${number} ${title}`;
      openTab({ repo, number, label });
      const to: Route = { kind: "issue", repo, number };
      focusTab.current = tabKey(to);
      navigate(to);
    },
    [openTab, navigate],
  );
  const openEpic = useCallback(
    (repo: string, number: number, title?: string) => {
      const label = title === undefined ? `${repo}#${number}` : `${repo}#${number} ${title}`;
      openTab({ repo, number, label, kind: "epic" });
      const to: Route = { kind: "epic", repo, number };
      focusTab.current = tabKey(to);
      navigate(to);
    },
    [openTab, navigate],
  );
  const openRow = useCallback((row: IssueRow) => openIssue(row.repo, row.number, row.title), [openIssue]);

  /** An opened issue's (or epic's) tab follows its title. */
  const retitle = useCallback((repo: string, number: number, title: string, kind?: "epic") => {
    const label = `${repo}#${number} ${title}`;
    setTabs((prev) => {
      const at = prev.findIndex((t) => sameTab(t, { repo, number, kind }));
      if (at < 0 || prev[at]?.label === label) return prev;
      return prev.map((t, i) => (i === at ? { ...t, label } : t));
    });
  }, []);

  const closeTab = (tab: OpenTab) => {
    const at = tabs.findIndex((t) => sameTab(t, tab));
    const neighbour = tabs[at + 1] ?? tabs[at - 1] ?? null;
    setTabs((prev) => prev.filter((t) => !sameTab(t, tab)));
    setVisited((prev) => {
      const next = new Set(prev);
      next.delete(issueKey(tab));
      return next;
    });
    const next: Route = neighbour ? routeOfTab(neighbour) : { kind: "inbox" };
    focusTab.current = tabKey(next);
    if (isOpened(route) && sameTab(route, tab)) navigate(next);
  };

  const follow = (e: MouseEvent<HTMLAnchorElement>, to: Route) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // a new tab or window
    e.preventDefault();
    navigate(to);
  };

  const isShown = (to: Route) => tabKey(to) === tabKey(route);

  return (
    <div className={s.workspace}>
      <nav ref={nav} className={s.wtabs} aria-label="Workspace">
        {FIXED.map((f) => (
          <a
            key={f.kind}
            className={s.wtab}
            data-tab={f.kind}
            data-focus-home={route.kind === f.kind ? "" : undefined}
            href={routePath({ kind: f.kind })}
            aria-current={route.kind === f.kind ? "page" : undefined}
            onClick={(e) => follow(e, { kind: f.kind })}
          >
            {f.label}
          </a>
        ))}
        {tabs.map((t) => {
          const to = routeOfTab(t);
          const ref = refOf(t);
          return (
            <span key={issueKey(t)} className={s.wtabItem}>
              <a
                className={s.wtab}
                data-tab={tabKey(to)}
                data-focus-home={isShown(to) ? "" : undefined}
                href={routePath(to)}
                aria-current={isShown(to) ? "page" : undefined}
                onClick={(e) => follow(e, to)}
              >
                <span className={s.wtabLabel}>
                  <VisibleText text={t.label} />
                </span>
              </a>
              <button type="button" className={s.wtabClose} aria-label={`Close ${ref}`} onClick={() => closeTab(t)}>
                ✕
              </button>
            </span>
          );
        })}
      </nav>
      <div className={s.body}>
        {visited.has("inbox") ? (
          <div className={s.pane} hidden={route.kind !== "inbox"}>
            <Inbox repos={repos} onOpen={openRow} active={route.kind === "inbox"} />
          </div>
        ) : null}
        {visited.has("backlog") ? (
          <div className={s.pane} hidden={route.kind !== "backlog"}>
            <Backlog repos={repos} onOpen={openRow} active={route.kind === "backlog"} />
          </div>
        ) : null}
        {visited.has("epics") ? (
          <div className={s.pane} hidden={route.kind !== "epics"}>
            <Epics repos={repos} onOpenIssue={openIssue} onOpenEpic={openEpic} />
          </div>
        ) : null}
        {visited.has("queues") ? (
          <div className={s.pane} hidden={route.kind !== "queues"}>
            <Queues />
          </div>
        ) : null}
        {tabs
          .filter((t) => visited.has(issueKey(t)))
          .map((t) => (
            <div key={issueKey(t)} className={s.pane} hidden={!isShown(routeOfTab(t))}>
              {t.kind === "epic" ? (
                <EpicTab
                  repo={t.repo}
                  number={t.number}
                  onTitle={(title) => retitle(t.repo, t.number, title, "epic")}
                  onClose={() => navigate({ kind: "epics" })}
                  onOpenIssue={openIssue}
                />
              ) : (
                <IssueView
                  repo={t.repo}
                  number={t.number}
                  repos={repos}
                  onTitle={(title) => retitle(t.repo, t.number, title)}
                  onOpenRef={(repo, number) => openIssue(repo, number)}
                  onOpenEpic={(repo, number) => openEpic(repo, number)}
                />
              )}
            </div>
          ))}
      </div>
    </div>
  );
}

export function App({ client }: { client?: JesiniaClient }) {
  return (
    <AppShell
      app={{ id: "issues", name: "Issues", glyph: "/issues/icons/monochrome.svg" }}
      {...(client ? { client } : {})}
    >
      <div className={s.app} data-density="compact">
        <TrayProvider>
          <Workspace />
        </TrayProvider>
      </div>
    </AppShell>
  );
}
