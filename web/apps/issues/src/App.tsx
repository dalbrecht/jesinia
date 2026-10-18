// SPDX-License-Identifier: AGPL-3.0-or-later
// The Issues app (Issues spec §6): workspace tabs (Inbox, Backlog, Epics and Queues, then one tab
// per opened issue) over the list or the opened issue, with the Pending changes tray pinned at the
// bottom whenever it isn't empty. Routes: /issues/, /issues/backlog, /issues/epics,
// /issues/queues and /issues/<owner>/<repo>/<n>. Compact density (ADR 0008).
import type { JesiniaClient } from "@jesinia/client";
import { useToast } from "@jesinia/design";
import { AppShell, useClient, VisibleText } from "@jesinia/shell";
import { useCallback, useEffect, useState, type MouseEvent } from "react";
import { errorCause, getRepos, refOf, routeOf, routePath, type IssueRow, type Repo, type Route } from "./api";
import { Backlog } from "./Backlog";
import { Inbox } from "./Inbox";
import s from "./issues.module.css";
import { IssueView } from "./IssueView";
import { loadTabs, sameTab, saveTabs, type OpenTab } from "./tabs";
import { TrayProvider, useTray } from "./Tray";

export { IDLE_POLL_MS, SYNCING_POLL_MS } from "./api";

type Fixed = "inbox" | "backlog" | "epics" | "queues";
const FIXED: { kind: Fixed; label: string }[] = [
  { kind: "inbox", label: "Inbox" },
  { kind: "backlog", label: "Backlog" },
  { kind: "epics", label: "Epics" },
  { kind: "queues", label: "Queues" },
];

const tabKey = (route: Route) => (route.kind === "issue" ? `${route.repo}#${route.number}` : route.kind);

function Workspace() {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const [route, setRoute] = useState<Route>(() => routeOf(window.location.pathname));
  const [tabs, setTabs] = useState<OpenTab[]>(() => {
    const saved = loadTabs();
    const linked = routeOf(window.location.pathname);
    if (linked.kind === "issue" && !saved.some((t) => sameTab(t, linked))) {
      saved.push({ repo: linked.repo, number: linked.number, label: `${linked.repo}#${linked.number}` });
    }
    return saved;
  });
  const [repos, setRepos] = useState<Repo[]>([]);
  // A list or an issue mounts the first time its tab is shown, then stays mounted (hidden while
  // another tab is shown) until its tab closes: it keeps its place, selection and scroll.
  const [visited, setVisited] = useState<ReadonlySet<string>>(
    () => new Set([tabKey(routeOf(window.location.pathname))]),
  );

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
      if (to.kind === "issue") openTab({ repo: to.repo, number: to.number, label: `${to.repo}#${to.number}` });
      show(to);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [openTab, show]);

  const openIssue = useCallback(
    (repo: string, number: number, title?: string) => {
      const label = title === undefined ? `${repo}#${number}` : `${repo}#${number} ${title}`;
      openTab({ repo, number, label });
      navigate({ kind: "issue", repo, number });
    },
    [openTab, navigate],
  );
  const openRow = useCallback((row: IssueRow) => openIssue(row.repo, row.number, row.title), [openIssue]);

  /** An opened issue's tab follows its title. */
  const retitle = useCallback((repo: string, number: number, title: string) => {
    const label = `${repo}#${number} ${title}`;
    setTabs((prev) => {
      const at = prev.findIndex((t) => sameTab(t, { repo, number }));
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
      next.delete(`${tab.repo}#${tab.number}`);
      return next;
    });
    if (route.kind === "issue" && sameTab(route, tab)) {
      navigate(neighbour ? { kind: "issue", repo: neighbour.repo, number: neighbour.number } : { kind: "inbox" });
    }
  };

  const follow = (e: MouseEvent<HTMLAnchorElement>, to: Route) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // a new tab or window
    e.preventDefault();
    navigate(to);
  };

  const isShown = (to: Route) => tabKey(to) === tabKey(route);

  return (
    <div className={s.workspace}>
      <nav className={s.wtabs} aria-label="Workspace">
        {FIXED.map((f) => (
          <a
            key={f.kind}
            className={s.wtab}
            href={routePath({ kind: f.kind })}
            aria-current={route.kind === f.kind ? "page" : undefined}
            onClick={(e) => follow(e, { kind: f.kind })}
          >
            {f.label}
          </a>
        ))}
        {tabs.map((t) => {
          const to: Route = { kind: "issue", repo: t.repo, number: t.number };
          const ref = refOf(t);
          return (
            <span key={ref} className={s.wtabItem}>
              <a
                className={s.wtab}
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
            <Inbox repos={repos} onOpen={openRow} />
          </div>
        ) : null}
        {visited.has("backlog") ? (
          <div className={s.pane} hidden={route.kind !== "backlog"}>
            <Backlog repos={repos} onOpen={openRow} />
          </div>
        ) : null}
        {route.kind === "epics" ? (
          <p className={s.empty}>Epics aren't in this version yet: they arrive with their list, briefs and drops.</p>
        ) : null}
        {route.kind === "queues" ? (
          <p className={s.empty}>Queues aren't in this version yet: they arrive with ordering and Done.</p>
        ) : null}
        {tabs
          .filter((t) => visited.has(`${t.repo}#${t.number}`))
          .map((t) => (
            <div key={refOf(t)} className={s.pane} hidden={!isShown({ kind: "issue", repo: t.repo, number: t.number })}>
              <IssueView
                repo={t.repo}
                number={t.number}
                repos={repos}
                onTitle={(title) => retitle(t.repo, t.number, title)}
                onOpenRef={(repo, number) => openIssue(repo, number)}
              />
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
