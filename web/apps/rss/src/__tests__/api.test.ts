// SPDX-License-Identifier: AGPL-3.0-or-later
import { fakeClient } from "@jesinia/shell";
import { describe, expect, it } from "vitest";
import {
  capCount,
  causeThen,
  fullArticle,
  getCounts,
  getRssSettings,
  importOpml,
  listFeeds,
  listPosts,
  markAllRead,
  markReadAbove,
  MAX_READ_IDS,
  OPML_EXPORT_URL,
  putRssSettings,
  readingMinutes,
  removeFeed,
  RssRequestError,
  retryArchive,
  safeHttpUrl,
  searchPosts,
  setRead,
  setReadMany,
  setRetention,
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

describe("feed management calls", () => {
  it("sends the bulk, retention, OPML and settings requests", async () => {
    const { client, calls } = fakeClient({
      "POST /api/rss/posts/mark-read": { marked: 2, ids: [5, 6] },
      "POST /api/rss/posts/read-state": { marked: 2 },
      "PUT /api/rss/feeds/3/retention": { id: 3 },
      "POST /api/rss/opml": { added: 1 },
      "GET /api/rss/settings": { a: 1 },
      "PUT /api/rss/settings": { a: 2 },
    });
    expect(await markAllRead(client, { feed: 3, folder: null }, "2026-10-21T00:00:00Z")).toEqual([5, 6]);
    expect(await setReadMany(client, [5, 6], false)).toBe(2);
    await setRetention(client, 3, { unit: "days", value: 30 });
    await setRetention(client, 3, null);
    await importOpml(client, "<opml/>");
    await getRssSettings(client);
    await putRssSettings(client, { a: 2 } as never);
    expect(OPML_EXPORT_URL).toBe("/api/rss/opml");
    const writes = calls.filter((c) => c.method !== "GET");
    expect(writes).toHaveLength(6);
    for (const w of writes) expect(w.headers.get("Idempotency-Key")).toMatch(/[0-9a-f-]{36}/);
    expect(await writes[0]?.clone().json()).toEqual({ feed: 3, folder: null, as_of: "2026-10-21T00:00:00Z" });
    expect(await writes[1]?.clone().json()).toEqual({ ids: [5, 6], read: false });
    expect(await writes[2]?.clone().json()).toEqual({ retention: { unit: "days", value: 30 } });
    expect(await writes[3]?.clone().json()).toEqual({ retention: null });
    expect(await writes[4]?.clone().json()).toEqual({ opml: "<opml/>" });
    expect(await writes[5]?.clone().json()).toEqual({ a: 2 });
  });

  it("sends a large Undo in batches the server accepts, each its own action", async () => {
    expect(MAX_READ_IDS).toBe(100_000);
    const { client, calls } = fakeClient({
      "POST /api/rss/posts/read-state": async (req: Request) => {
        const { ids } = (await req.clone().json()) as { ids: number[] };
        return Response.json({ marked: ids.length - 1 });
      },
    });
    const ids = Array.from({ length: 2 * MAX_READ_IDS + 5 }, (_, i) => i + 1);
    expect(await setReadMany(client, ids, false)).toBe(ids.length - 3);
    expect(calls).toHaveLength(3);
    const bodies = (await Promise.all(calls.map((c) => c.clone().json()))) as { ids: number[]; read: boolean }[];
    expect(bodies.map((b) => b.ids.length)).toEqual([MAX_READ_IDS, MAX_READ_IDS, 5]);
    expect(bodies.flatMap((b) => b.ids)).toEqual(ids);
    expect(bodies.every((b) => !b.read)).toBe(true);
    const keys = calls.map((c) => c.headers.get("Idempotency-Key"));
    expect(new Set(keys).size).toBe(3);
  });

  it("an empty Undo sends nothing", async () => {
    const { client, calls } = fakeClient({});
    expect(await setReadMany(client, [], false)).toBe(0);
    expect(calls).toHaveLength(0);
  });

  it("a remove can outlive the page (keepalive), keeping its idempotency key", async () => {
    const { client, calls } = fakeClient({ "DELETE /api/rss/feeds/3": () => new Response(null, { status: 204 }) });
    await removeFeed(client, 3, { keepalive: true });
    await removeFeed(client, 3);
    expect(calls.map((c) => c.keepalive)).toEqual([true, false]);
    for (const c of calls) expect(c.headers.get("Idempotency-Key")).toMatch(/[0-9a-f-]{36}/);
  });

  it("surfaces the server's cause on failure", async () => {
    const { client } = fakeClient({
      "POST /api/rss/posts/mark-read": fail(400, "bad time"),
      "POST /api/rss/posts/read-state": fail(400, "too many"),
      "PUT /api/rss/feeds/3/retention": fail(422, "days 1-365"),
      "POST /api/rss/opml": fail(400, "not OPML"),
      "GET /api/rss/settings": fail(500, "settings down"),
      "PUT /api/rss/settings": fail(500, "cannot save"),
    });
    await expect(markAllRead(client, { feed: null, folder: null }, "x")).rejects.toThrow("bad time");
    await expect(setReadMany(client, [1], true)).rejects.toThrow("too many");
    await expect(setRetention(client, 3, null)).rejects.toThrow("days 1-365");
    await expect(importOpml(client, "x")).rejects.toThrow("not OPML");
    await expect(getRssSettings(client)).rejects.toThrow("settings down");
    await expect(putRssSettings(client, {} as never)).rejects.toThrow("cannot save");
  });
});

describe("causeThen", () => {
  it("adds the next step to the server's cause, not to an unanswered request's own fix", () => {
    expect(causeThen(new RssRequestError("no feed 2", 404), "Try again.")).toBe("no feed 2. Try again.");
    expect(causeThen(new TypeError("Failed to fetch"), "Try again.")).toBe(
      "Jesinia didn't answer (Failed to fetch). Check that Jesinia is running, then try again.",
    );
  });
});
