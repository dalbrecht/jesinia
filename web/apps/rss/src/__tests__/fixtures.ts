// SPDX-License-Identifier: AGPL-3.0-or-later
import { baseRoutes, type Routes } from "@jesinia/shell";
import type { Counts, Feed, Post } from "../api";

export function post(id: number, over: Partial<Post> = {}): Post {
  return {
    id,
    feed_id: 2,
    feed_title: "Fly.io blog",
    url: `https://fly.io/blog/post-${id}`,
    title: `Post ${id}`,
    author: "Ada",
    published_at: "2026-10-14T09:00:00Z",
    first_seen_at: "2026-10-14T09:05:00Z",
    snippet: `Snippet ${id}`,
    content_html: `<p>Body of post ${id}.</p>`,
    discussion_url: null,
    is_summary: false,
    read: false,
    kept_page_id: null,
    archive_state: null,
    archive_error: null,
    has_full_article: false,
    ...over,
  };
}

const HEALTHY = { last_ok_at: null, fail_count: 0, last_error: null, newest_post_at: null, retention: null };

export const FEEDS: Feed[] = [
  { id: 1, title: "Hacker News", url: "https://hnrss.org/frontpage", folder: "News", folder_id: 10, unread: 1200, warning: null, ...HEALTHY },
  { id: 2, title: "Fly.io blog", url: "https://fly.io/blog/feed.xml", folder: "Engineering", folder_id: 11, unread: 4, warning: null, ...HEALTHY },
  { id: 3, title: "Old Blog", url: "https://old.example/feed", folder: "Engineering", folder_id: 11, unread: 0, warning: "Failing for 3 days: 404 from old.example", ...HEALTHY },
  { id: 4, title: "Loose feed", url: "https://loose.example/feed", folder: null, folder_id: null, unread: 2, warning: null, ...HEALTHY },
]; // prettier-ignore

export const COUNTS: Counts = {
  feeds: [
    { id: 1, unread: 1200 },
    { id: 2, unread: 4 },
    { id: 3, unread: 0 },
    { id: 4, unread: 2 },
  ],
  folders: [
    { id: 10, unread: 1200 },
    { id: 11, unread: 4 },
  ],
  views: { unread: 1206, kept: 31, all: 1500 },
};

export const POSTS: Post[] = [post(1), post(2), post(3, { read: true }), post(4)];

/** PATCH answers with the post as the request left it. */
const echo = (id: number) => async (req: Request) =>
  Response.json(post(id, (await req.clone().json()) as Partial<Post>));

export function routes(over: Routes = {}): Routes {
  return baseRoutes({
    "GET /api/rss/feeds": FEEDS,
    "GET /api/rss/counts": COUNTS,
    "GET /api/rss/posts": { items: POSTS, next_before: null },
    ...Object.fromEntries(POSTS.map((p) => [`PATCH /api/rss/posts/${p.id}`, echo(p.id)])),
    "POST /api/rss/posts/read-above": { marked: 2 },
    ...over,
  });
}
