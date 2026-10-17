// SPDX-License-Identifier: AGPL-3.0-or-later
// The post list (spec §2.1): headline, snippet, source and age, newest first. Only the rows
// in view (plus a margin) are in the DOM, so a feed with thousands of posts stays fast.
import { Badge, Row } from "@jesinia/design";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { Post } from "./api";
import s from "./rss.module.css";

export const ROW_HEIGHT = 76;
const OVERSCAN = 6;
/** Before layout (and in jsdom, which has none) assume a tall window. */
const FALLBACK_HEIGHT = 720;

export type ArchiveStatus = "archiving" | "done" | "failed" | null;

/** A kept post with no archive state was kept before archiving existed: it is in the KB. */
export function archiveStatus(post: Post): ArchiveStatus {
  if (post.archive_state === "archiving" || post.archive_state === "failed") return post.archive_state;
  return post.kept_page_id || post.archive_state === "done" ? "done" : null;
}

export function ArchiveBadge({ status }: { status: ArchiveStatus }) {
  if (status === "archiving") return <span className={s.archiving}>Archiving…</span>;
  if (status === "done") return <Badge tone="fern">In KB</Badge>;
  if (status === "failed") {
    return (
      <Badge tone="warn" icon="!">
        Archive incomplete
      </Badge>
    );
  }
  return null;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "5m", "2h", "3d", then the date. */
export function age(iso: string, now: number = Date.now()): string {
  const ms = now - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "";
  if (ms < HOUR) return `${Math.max(0, Math.floor(ms / MINUTE))}m`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h`;
  if (ms < 30 * DAY) return `${Math.floor(ms / DAY)}d`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export type PostListProps = {
  posts: Post[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  /** The last rows are in view: load the next page if there is one. */
  onEndReached: () => void;
  empty: ReactNode;
};

export function PostList({ posts, selectedId, onSelect, onEndReached, empty }: PostListProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(FALLBACK_HEIGHT);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const measure = () => setHeight(el.clientHeight || FALLBACK_HEIGHT);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const total = posts.length;
  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const last = Math.min(total, Math.ceil((scrollTop + height) / ROW_HEIGHT) + OVERSCAN);

  // Keep the selection in view as j/k move it.
  const selectedIndex = posts.findIndex((p) => p.id === selectedId);
  useEffect(() => {
    const el = scroller.current;
    if (!el || selectedIndex < 0) return;
    const top = selectedIndex * ROW_HEIGHT;
    let next: number | null = null;
    if (top < el.scrollTop) next = top;
    else if (top + ROW_HEIGHT > el.scrollTop + height) next = top + ROW_HEIGHT - height;
    if (next === null) return;
    el.scrollTop = next;
    setScrollTop(next);
  }, [selectedIndex, height]);

  useEffect(() => {
    if (total > 0 && last >= total - OVERSCAN) onEndReached();
  }, [last, total, onEndReached]);

  return (
    <div
      ref={scroller}
      className={s.scroller}
      data-testid="post-scroller"
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
    >
      {total === 0 ? (
        <p className={s.empty}>{empty}</p>
      ) : (
        <div
          role="listbox"
          aria-label="Posts"
          tabIndex={0}
          className={s.listbox}
          style={{ height: total * ROW_HEIGHT }}
          {...(selectedIndex >= 0 ? { "aria-activedescendant": `post-${selectedId}` } : {})}
        >
          {posts.slice(first, last).map((post, i) => {
            const index = first + i;
            return (
              <div
                key={post.id}
                id={`post-${post.id}`}
                role="option"
                aria-selected={post.id === selectedId}
                aria-posinset={index + 1}
                aria-setsize={total}
                className={s.option}
                style={{ top: index * ROW_HEIGHT, height: ROW_HEIGHT }}
                onClick={() => onSelect(post.id)}
              >
                <Row
                  title={post.title}
                  excerpt={post.snippet}
                  meta={
                    <span className={s.rowMeta}>
                      {post.feed_title}
                      <ArchiveBadge status={archiveStatus(post)} />
                    </span>
                  }
                  trailing={<span className={s.age}>{age(post.published_at ?? post.first_seen_at)}</span>}
                  unread={!post.read}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
