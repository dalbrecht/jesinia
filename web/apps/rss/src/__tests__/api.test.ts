// SPDX-License-Identifier: AGPL-3.0-or-later
import { fakeClient } from "@jesinia/shell";
import { describe, expect, it } from "vitest";
import {
  capCount,
  fullArticle,
  getCounts,
  listFeeds,
  listPosts,
  markReadAbove,
  needsFullArticle,
  readingMinutes,
  RssRequestError,
  retryArchive,
  safeHttpUrl,
  searchPosts,
  setRead,
} from "../api";
import { post } from "./fixtures";

const fail =
  (status: number, detail = "boom") =>
  () =>
    Response.json({ error: "X", detail }, { status });

describe("helpers", () => {
  it("caps unread counts at 999+", () => {
    expect(capCount(0)).toBe("0");
    expect(capCount(999)).toBe("999");
    expect(capCount(1000)).toBe("999+");
    expect(capCount(12_345)).toBe("999+");
  });

  it("accepts only http(s) URLs, resolved against a base", () => {
    expect(safeHttpUrl("https://a.example/x")).toBe("https://a.example/x");
    expect(safeHttpUrl("/x", "http://a.example/y/z")).toBe("http://a.example/x");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,x")).toBeNull();
    expect(safeHttpUrl("/relative")).toBeNull();
    expect(safeHttpUrl("/x", "javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("")).toBeNull();
  });

  it("offers the full article for summaries and short content", () => {
    expect(needsFullArticle(post(1, { is_summary: true, content_html: "x".repeat(5000) }))).toBe(true);
    expect(needsFullArticle(post(1, { content_html: null }))).toBe(true);
    expect(needsFullArticle(post(1, { content_html: "<p>short</p>" }))).toBe(true);
    expect(needsFullArticle(post(1, { content_html: "x".repeat(1500) }))).toBe(false);
  });

  it("estimates reading time at 230 words a minute, at least one", () => {
    expect(readingMinutes("")).toBe(1);
    expect(readingMinutes("word ".repeat(460))).toBe(2);
  });
});

describe("calls", () => {
  it("lists posts for a scope with a cursor and the page size", async () => {
    const { client, calls } = fakeClient({ "GET /api/rss/posts": { items: [post(1)], next_before: "c" } });
    const page = await listPosts(client, { view: "all", feed: 3, folder: null }, "cur");
    expect(page.items).toHaveLength(1);
    const url = new URL(calls[0]?.url ?? "");
    expect(url.searchParams.get("view")).toBe("all");
    expect(url.searchParams.get("feed")).toBe("3");
    expect(url.searchParams.get("folder")).toBeNull();
    expect(url.searchParams.get("before")).toBe("cur");
    expect(url.searchParams.get("limit")).toBe("100");
  });

  it("every call surfaces the server's cause on failure", async () => {
    const { client } = fakeClient({
      "GET /api/rss/feeds": fail(500, "feeds down"),
      "GET /api/rss/posts": fail(400, "bad cursor"),
      "GET /api/rss/counts": fail(500),
      "PATCH /api/rss/posts/1": fail(404, "gone"),
      "POST /api/rss/posts/read-above": fail(400, "bad time"),
      "GET /api/rss/posts/1/article": fail(502, "The site answered 403."),
      "GET /api/rss/posts/search": fail(500, "search down"),
      "POST /api/rss/posts/1/archive": fail(409, "not failed"),
    });
    const scope = { view: "unread" as const, feed: null, folder: null };
    await expect(listFeeds(client)).rejects.toThrow("feeds down");
    await expect(listPosts(client, scope)).rejects.toThrow("bad cursor");
    await expect(getCounts(client)).rejects.toBeInstanceOf(RssRequestError);
    await expect(setRead(client, 1, true)).rejects.toThrow("gone");
    await expect(markReadAbove(client, 1, scope, "2026-10-16T00:00:00Z")).rejects.toThrow("bad time");
    await expect(fullArticle(client, 1)).rejects.toThrow("The site answered 403.");
    await expect(searchPosts(client, "q")).rejects.toThrow("search down");
    await expect(retryArchive(client, 1)).rejects.toThrow("not failed");
  });

  it("a failure without a detail names the status", async () => {
    const { client } = fakeClient({ "GET /api/rss/feeds": () => new Response("nope", { status: 503 }) });
    await expect(listFeeds(client)).rejects.toThrow("HTTP 503");
  });

  it("writes send an idempotency key and the read-above scope", async () => {
    const { client, calls } = fakeClient({
      "PATCH /api/rss/posts/1": post(1, { read: false }),
      "POST /api/rss/posts/read-above": { marked: 4 },
      "POST /api/rss/posts/1/archive": post(1, { archive_state: "archiving" }),
      "GET /api/rss/posts/1/article": { markdown: "# Full" },
      "GET /api/rss/posts/search": [post(2)],
    });
    expect((await setRead(client, 1, false)).read).toBe(false);
    expect(await markReadAbove(client, 1, { view: "kept", feed: null, folder: 7 }, "2026-10-16T10:00:00.000Z")).toBe(4);
    expect((await retryArchive(client, 1)).archive_state).toBe("archiving");
    expect(await fullArticle(client, 1)).toBe("# Full");
    expect(await searchPosts(client, "sqlite", new AbortController().signal)).toHaveLength(1);
    const writes = calls.filter((c) => c.method !== "GET");
    expect(writes).toHaveLength(3);
    for (const w of writes) expect(w.headers.get("Idempotency-Key")).toMatch(/[0-9a-f-]{36}/);
    expect(await writes[0]?.clone().json()).toEqual({ read: false });
    expect(await writes[1]?.clone().json()).toEqual({
      post_id: 1,
      view: "kept",
      feed: null,
      folder: 7,
      as_of: "2026-10-16T10:00:00.000Z",
    });
  });
});
