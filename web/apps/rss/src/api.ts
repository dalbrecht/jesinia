// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes and calls the RSS PWA uses (`/api/rss`, spec §2).
import { errorText, withIdempotency, type components, type JesiniaClient } from "@jesinia/client";

export { safeHttpUrl } from "@jesinia/shell";

export type Post = components["schemas"]["PostOut"];
export type Feed = components["schemas"]["FeedOut"];
export type Counts = components["schemas"]["CountsOut"];
export type PostPage = components["schemas"]["PostPageOut"];
export type Kept = components["schemas"]["KeepOut"];
export type Takeaway = components["schemas"]["TakeawayOut"];
export type EngageSession = components["schemas"]["SessionOut"];
export type View = "unread" | "kept" | "all";

/** The server's cap on a takeaway (`TAKEAWAY_MAX`). */
export const TAKEAWAY_MAX = 2000;

/** What Keep writes (spec §3.1); `takeaway` only from Engage, as the author left it. */
export type KeepRequest = { note: string; tags: string[]; takeaway?: { text: string; session: string } };

/** Which posts the list shows: a view, optionally narrowed to one feed or one folder. */
export type Scope = { view: View; feed: number | null; folder: number | null };

export const PAGE_SIZE = 100;
/** Feed content shorter than this is probably a teaser: offer the full article. */
export const SHORT_CONTENT_CHARS = 1500;
const WORDS_PER_MINUTE = 230;
const COUNT_CAP = 999;

/** A failed RSS request, carrying the server's cause (`detail`) as its message. */
export class RssRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "RssRequestError";
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

function unwrap<T>(r: Result<T>): T {
  if (!r.response.ok) {
    throw new RssRequestError(errorText(r.error, `HTTP ${r.response.status}`), r.response.status);
  }
  return r.data as T;
}

export async function listFeeds(client: JesiniaClient): Promise<Feed[]> {
  return unwrap(await client.GET("/api/rss/feeds"));
}

export async function getCounts(client: JesiniaClient): Promise<Counts> {
  return unwrap(await client.GET("/api/rss/counts"));
}

export async function listPosts(client: JesiniaClient, scope: Scope, before?: string): Promise<PostPage> {
  const query = {
    view: scope.view,
    limit: PAGE_SIZE,
    ...(scope.feed !== null ? { feed: scope.feed } : {}),
    ...(scope.folder !== null ? { folder: scope.folder } : {}),
    ...(before !== undefined ? { before } : {}),
  };
  return unwrap(await client.GET("/api/rss/posts", { params: { query } }));
}

export async function searchPosts(client: JesiniaClient, q: string, signal?: AbortSignal): Promise<Post[]> {
  return unwrap(await client.GET("/api/rss/posts/search", { params: { query: { q } }, ...(signal ? { signal } : {}) }));
}

export async function setRead(client: JesiniaClient, id: number, read: boolean): Promise<Post> {
  const r = await client.PATCH("/api/rss/posts/{post_id}", {
    params: { path: { post_id: id }, header: withIdempotency() },
    body: { read },
  });
  return unwrap(r);
}

/**
 * Mark the post and everything above it in `scope` read. `asOf` is when the list was loaded:
 * posts that arrived later were never on screen and stay unread.
 */
export async function markReadAbove(
  client: JesiniaClient,
  postId: number,
  scope: Scope,
  asOf: string,
): Promise<number> {
  const r = await client.POST("/api/rss/posts/read-above", {
    params: { header: withIdempotency() },
    body: { post_id: postId, view: scope.view, feed: scope.feed, folder: scope.folder, as_of: asOf },
  });
  return unwrap(r).marked;
}

/** The original article as Markdown; an `RssRequestError` with the cause when it can't be had. */
export async function fullArticle(client: JesiniaClient, id: number): Promise<string> {
  return unwrap(await client.GET("/api/rss/posts/{post_id}/article", { params: { path: { post_id: id } } })).markdown;
}

export async function retryArchive(client: JesiniaClient, id: number): Promise<Post> {
  const r = await client.POST("/api/rss/posts/{post_id}/archive", {
    params: { path: { post_id: id }, header: withIdempotency() },
  });
  return unwrap(r);
}

/** Subscribe to a feed URL, or to the feed a site advertises; `folder` is created if new. */
export async function addFeed(client: JesiniaClient, url: string, folder: string | null): Promise<Feed> {
  const r = await client.POST("/api/rss/feeds", {
    params: { header: withIdempotency() },
    body: { url, folder },
  });
  return unwrap(r);
}

/** Unsubscribe: unkept posts go, kept ones stay in the KB. Adding the URL again restores it. */
export async function removeFeed(client: JesiniaClient, id: number): Promise<void> {
  const r = await client.DELETE("/api/rss/feeds/{feed_id}", {
    params: { path: { feed_id: id }, header: withIdempotency() },
  });
  unwrap(r);
}

/** Put the feed in `folder` (created if new), or in none. */
export async function moveFeed(client: JesiniaClient, id: number, folder: string | null): Promise<Feed> {
  const r = await client.PATCH("/api/rss/feeds/{feed_id}", {
    params: { path: { feed_id: id }, header: withIdempotency() },
    body: { folder },
  });
  return unwrap(r);
}

export async function renameFolder(client: JesiniaClient, id: number, name: string): Promise<void> {
  const r = await client.PATCH("/api/rss/folders/{folder_id}", {
    params: { path: { folder_id: id }, header: withIdempotency() },
    body: { name },
  });
  unwrap(r);
}

/** Poll one feed now (a failing feed's Retry): the feed afterwards, with its warning if it still fails. */
export async function refreshFeed(client: JesiniaClient, id: number): Promise<Feed> {
  const r = await client.POST("/api/rss/feeds/{feed_id}/refresh", {
    params: { path: { feed_id: id }, header: withIdempotency() },
  });
  return unwrap(r);
}

export async function refreshFeeds(client: JesiniaClient): Promise<void> {
  unwrap(await client.POST("/api/rss/refresh", { params: { header: withIdempotency() } }));
}

/**
 * Write the post's KB page now; the archive then follows in the background
 * (`post.archive_state`). `key` names the intended action: a retry of the same request reuses
 * it, so a lost answer never keeps (and appends the note) twice.
 */
export async function keepPost(client: JesiniaClient, id: number, keep: KeepRequest, key: string): Promise<Kept> {
  const r = await client.POST("/api/rss/posts/{post_id}/keep", {
    params: { path: { post_id: id }, header: withIdempotency(key) },
    body: keep,
  });
  return unwrap(r);
}

/** A read-only agent session about the post (spec §3.2); chat in it through `/api/agents`. */
export async function engagePost(client: JesiniaClient, id: number, runner: string): Promise<EngageSession> {
  const r = await client.POST("/api/rss/posts/{post_id}/engage", {
    params: { path: { post_id: id }, header: withIdempotency() },
    body: { runner },
  });
  return unwrap(r);
}

/** The engaged agent's 1–3 sentence takeaway, for the author to edit; nothing is written. */
export async function askTakeaway(client: JesiniaClient, session: string): Promise<Takeaway> {
  const r = await client.POST("/api/rss/sessions/{session_id}/takeaway", {
    params: { path: { session_id: session }, header: withIdempotency() },
  });
  return unwrap(r);
}

/** Unread counts are capped in the UI (spec §2.3). */
export function capCount(n: number): string {
  return n > COUNT_CAP ? `${COUNT_CAP}+` : String(n);
}

export function needsFullArticle(post: Post): boolean {
  return post.is_summary || (post.content_html ?? "").length < SHORT_CONTENT_CHARS;
}

export function readingMinutes(text: string): number {
  const words = text.split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.round(words / WORDS_PER_MINUTE));
}
