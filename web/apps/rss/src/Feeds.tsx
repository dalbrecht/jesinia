// SPDX-License-Identifier: AGPL-3.0-or-later
// The feeds pane (spec §2.1): the views, then folders (collapsible) and their feeds, each
// with its unread count and, on hover or focus, Mark read. No sync errors here: ⚙ Manage
// feeds, at the bottom, counts the failing feeds and opens feed management.
import { Button } from "@jesinia/design";
import { useState } from "react";
import { capCount, type Counts, type Feed, type Scope } from "./api";
import s from "./rss.module.css";

/** Collapsed folder ids, per browser (a convenience: losing it costs nothing). */
export const COLLAPSED_KEY = "jesinia.rss.collapsed-folders";

function readCollapsed(): number[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((v): v is number => typeof v === "number") : [];
  } catch {
    return [];
  }
}

function writeCollapsed(ids: number[]): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(ids));
  } catch {
    // Private mode or a full quota: the folders still collapse for this session.
  }
}

type Folder = { id: number; name: string; feeds: Feed[] };

function group(feeds: Feed[]): { folders: Folder[]; loose: Feed[] } {
  const byId = new Map<number, Folder>();
  const loose: Feed[] = [];
  for (const feed of feeds) {
    if (feed.folder_id === null) {
      loose.push(feed);
      continue;
    }
    const folder = byId.get(feed.folder_id) ?? { id: feed.folder_id, name: feed.folder ?? "", feeds: [] };
    folder.feeds.push(feed);
    byId.set(feed.folder_id, folder);
  }
  const folders = [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  return { folders, loose };
}

function Count({ n }: { n: number }) {
  return n > 0 ? <span className={s.count}>{capCount(n)}</span> : null;
}

export type FeedsProps = {
  feeds: Feed[];
  counts: Counts | null;
  scope: Scope;
  onScope: (scope: Scope) => void;
  /** Mark everything in a feed or folder read; `label` names it in the toast. */
  onMarkRead: (target: { feed: number | null; folder: number | null }, label: string) => void;
  onManage: () => void;
};

export function Feeds({ feeds, counts, scope, onScope, onMarkRead, onManage }: FeedsProps) {
  const [collapsed, setCollapsed] = useState<number[]>(readCollapsed);
  const { folders, loose } = group(feeds);
  const unreadOf = (list: { id: number; unread: number }[] | undefined, id: number, fallback = 0) =>
    list?.find((c) => c.id === id)?.unread ?? fallback;
  const toggle = (id: number) => {
    const next = collapsed.includes(id) ? collapsed.filter((c) => c !== id) : [...collapsed, id];
    setCollapsed(next);
    writeCollapsed(next);
  };
  const scoped = scope.feed === null && scope.folder === null;
  const current = (on: boolean) => (on ? ("true" as const) : undefined);

  const failing = feeds.filter((f) => f.warning !== null).length;
  /** Mark read, beside a row: shown on the row's hover or focus; never selects the row. */
  const markRead = (target: { feed: number | null; folder: number | null }, label: string) => (
    <button
      type="button"
      className={s.rowMark}
      aria-label={`Mark ${label} read`}
      onClick={(e) => {
        e.stopPropagation();
        onMarkRead(target, label);
      }}
    >
      Mark read
    </button>
  );

  const feedRow = (feed: Feed) => (
    <div key={feed.id} className={s.row}>
      <button
        type="button"
        className={s.feed}
        aria-current={current(scope.feed === feed.id)}
        onClick={() => onScope({ view: scope.view === "kept" ? "unread" : scope.view, feed: feed.id, folder: null })}
      >
        <span className={s.feedName}>{feed.title}</span>
        <Count n={unreadOf(counts?.feeds, feed.id, feed.unread)} />
      </button>
      {markRead({ feed: feed.id, folder: null }, feed.title)}
    </div>
  );

  return (
    <nav className={s.feeds} aria-label="Feeds">
      <button
        type="button"
        className={s.feed}
        aria-current={current(scoped && scope.view !== "kept")}
        onClick={() => onScope({ view: "unread", feed: null, folder: null })}
      >
        <span className={s.feedName}>All unread</span>
        <Count n={counts?.views.unread ?? 0} />
      </button>
      <button
        type="button"
        className={s.feed}
        aria-current={current(scoped && scope.view === "kept")}
        onClick={() => onScope({ view: "kept", feed: null, folder: null })}
      >
        <span className={s.feedName}>In KB</span>
        <Count n={counts?.views.kept ?? 0} />
      </button>
      {folders.map((folder) => {
        const open = !collapsed.includes(folder.id);
        return (
          <div key={folder.id} className={s.folder}>
            <div className={`${s.folderHead} ${s.row}`}>
              <button
                type="button"
                className={s.toggle}
                aria-label={`Feeds in ${folder.name}`}
                aria-expanded={open}
                onClick={() => toggle(folder.id)}
              >
                <span aria-hidden="true">{open ? "▾" : "▸"}</span>
              </button>
              <button
                type="button"
                className={s.folderName}
                aria-current={current(scope.folder === folder.id)}
                onClick={() =>
                  onScope({ view: scope.view === "kept" ? "unread" : scope.view, feed: null, folder: folder.id })
                }
              >
                <span className={s.feedName}>{folder.name}</span>
                {/* Open, its feeds show their own counts (spec §2.3: collapsing quiets them). */}
                {open ? null : <Count n={unreadOf(counts?.folders, folder.id)} />}
              </button>
              {markRead({ feed: null, folder: folder.id }, folder.name)}
            </div>
            {open ? folder.feeds.map(feedRow) : null}
          </div>
        );
      })}
      {loose.length ? <div className={s.folder}>{loose.map(feedRow)}</div> : null}
      <div className={s.addFeedRow}>
        <Button variant="ghost" size="sm" onClick={onManage}>
          {failing ? `⚙ Manage feeds · ${failing}` : "⚙ Manage feeds"}
        </Button>
      </div>
    </nav>
  );
}
