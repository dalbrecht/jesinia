// SPDX-License-Identifier: AGPL-3.0-or-later
// Surviving refreshes and restarts (R29, R30): a reload of the same list keeps its rows and the
// open post (with Keep's typed note and the Engage chat), and every (re)connect of the live
// stream refetches the list and counts.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Post } from "../api";
import { POSTS, post, routes } from "./fixtures";

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {}
}

const SESSION = {
  id: "s9",
  runner: "claude-code",
  status: "idle",
  model: null,
  read_only: true,
  subject: { kind: "rss", ref: "1", title: "Post 1" },
};

/** The list as the server answers: post 1 drops out of All unread once it is read. */
function unreadRoutes(over: Routes = {}) {
  let read = false;
  return routes({
    "GET /api/rss/posts": () =>
      Response.json({ items: read ? POSTS.filter((p) => p.id !== 1) : POSTS, next_before: null }),
    "PATCH /api/rss/posts/1": () => {
      read = true;
      return Response.json(post(1, { read: true }));
    },
    "POST /api/rss/refresh": { polled: 4, new_items: 0, failed: 0 },
    "POST /api/kb/tags/suggest": [],
    "POST /api/rss/posts/1/engage": () => Response.json(SESSION, { status: 201 }),
    ...over,
  });
}

function setup(over: Routes = {}) {
  const env = fakeClient(unreadRoutes(over));
  render(<App client={env.client} />);
  return env;
}

const gets = (calls: Request[], path: string) =>
  calls.filter((c) => c.method === "GET" && new URL(c.url).pathname === path);
const refresh = () => userEvent.click(screen.getByRole("button", { name: "↻ Refresh" }));
const stream = () => FakeSource.all.find((s) => s.url.includes("rss.post_changed"));

async function openFirst() {
  await userEvent.click(await screen.findByRole("option", { name: /Post 1/ }));
  await screen.findByRole("heading", { name: "Post 1", level: 1 });
}

beforeEach(() => {
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => vi.unstubAllGlobals());

describe("A reload of the same list (R30)", () => {
  it("keeps the open post and Keep's typed note, even though the post is now read", async () => {
    const { calls } = setup();
    await openFirst();
    await userEvent.keyboard("n");
    const note = await screen.findByRole("textbox", { name: "Note" });
    await userEvent.type(note, "Worth keeping");
    const before = gets(calls, "/api/rss/posts").length;
    await refresh();
    await waitFor(() => expect(gets(calls, "/api/rss/posts").length).toBeGreaterThan(before));
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "Post 1", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveValue("Worth keeping");
  });

  it("keeps the Engage chat open across a Refresh", async () => {
    const { calls } = setup();
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Engage" }));
    await screen.findByRole("complementary", { name: "Chat about this post" });
    const before = gets(calls, "/api/rss/posts").length;
    await refresh();
    await waitFor(() => expect(gets(calls, "/api/rss/posts").length).toBeGreaterThan(before));
    await act(async () => {});
    expect(screen.getByRole("complementary", { name: "Chat about this post" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Post 1", level: 1 })).toBeInTheDocument();
  });

  it("lets the read post go once the reader moves on", async () => {
    setup();
    await openFirst();
    await refresh();
    await act(async () => {});
    await userEvent.keyboard("j"); // to Post 2
    await screen.findByRole("heading", { name: "Post 2", level: 1 });
    await refresh();
    await waitFor(() => expect(screen.queryByRole("option", { name: /Post 1/ })).toBeNull());
  });

  it("a search that leaves the open post out lets it go", async () => {
    setup({ "GET /api/rss/posts/search": [post(2)] });
    await openFirst();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search headlines" }), "two");
    expect(await screen.findByText("Choose a post to read it here.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Post 1", level: 1 })).toBeNull();
  });

  it("shows the current rows, never a blank Loading…, while the reload is on its way", async () => {
    let answer: (r: Response) => void = () => {};
    let held = false;
    setup({
      "GET /api/rss/posts": () =>
        held
          ? new Promise<Response>((resolve) => (answer = resolve))
          : Response.json({ items: POSTS, next_before: null }),
    });
    await screen.findByRole("option", { name: /Post 1/ });
    held = true;
    await refresh();
    await waitFor(() => expect(answer).not.toBe(undefined));
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(within(screen.getByRole("listbox", { name: "Posts" })).getAllByRole("option")).toHaveLength(POSTS.length);
    await act(async () => answer(Response.json({ items: [post(9)], next_before: null })));
    expect(await screen.findByRole("option", { name: /Post 9/ })).toBeInTheDocument();
  });

  it("an empty list reloads without flashing Loading…", async () => {
    let answer: ((r: Response) => void) | null = null;
    let held = false;
    setup({
      "GET /api/rss/posts": () =>
        held ? new Promise<Response>((resolve) => (answer = resolve)) : Response.json({ items: [], next_before: null }),
    });
    expect(await screen.findByText("Nothing unread here.")).toBeInTheDocument();
    held = true;
    await refresh();
    await waitFor(() => expect(answer).not.toBeNull());
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.getByText("Nothing unread here.")).toBeInTheDocument();
  });

  it("a failed reload keeps the rows and says why", async () => {
    let fail = false;
    setup({
      "GET /api/rss/posts": () =>
        fail
          ? Response.json({ detail: "rss module not started" }, { status: 503 })
          : Response.json({ items: POSTS, next_before: null }),
    });
    await screen.findByRole("option", { name: /Post 1/ });
    fail = true;
    await refresh();
    expect((await screen.findAllByText(/Couldn't reload posts: rss module not started\. /)).length).toBeGreaterThan(0);
    expect(within(screen.getByRole("listbox", { name: "Posts" })).getAllByRole("option")).toHaveLength(POSTS.length);
    expect(screen.queryByRole("alert", { name: /Couldn't load posts/ })).toBeNull();
  });
});

describe("The live stream across a restart (R29)", () => {
  it("refetches the list and counts on every (re)connect, and applies events after a restart", async () => {
    const kept = { kept_page_id: "pg1", archive_state: "archiving" } as const;
    const { calls } = setup({
      "GET /api/rss/posts": () => Response.json({ items: [post(1, kept)], next_before: null }),
      "PATCH /api/rss/posts/1": () => Response.json(post(1, { ...kept, read: true })),
    });
    await openFirst();
    const reader = () => screen.getByRole("region", { name: "Reader" });
    expect(within(reader()).getByText("Archiving…")).toBeInTheDocument();
    const posts = gets(calls, "/api/rss/posts").length;
    const counts = gets(calls, "/api/rss/counts").length;
    const first = stream();
    if (!first) throw new Error("no rss.post_changed stream");
    act(() => first.onopen?.());
    await waitFor(() => expect(gets(calls, "/api/rss/posts").length).toBe(posts + 1));
    expect(gets(calls, "/api/rss/counts").length).toBe(counts + 1);
    // The server restarts: the stream errors, then reconnects and opens again.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    act(() => first.onerror?.());
    act(() => vi.advanceTimersByTime(2000));
    vi.useRealTimers();
    const second = FakeSource.all.filter((s) => s.url.includes("rss.post_changed")).at(-1) as FakeSource;
    expect(second).not.toBe(first);
    act(() => second.onopen?.());
    await waitFor(() => expect(gets(calls, "/api/rss/posts").length).toBe(posts + 2));
    expect(gets(calls, "/api/rss/counts").length).toBe(counts + 2);
    // The new boot's ids are higher; its archive news lands.
    act(() =>
      second.listeners.get("rss.post_changed")?.(
        new MessageEvent("rss.post_changed", {
          data: JSON.stringify({ id: 1, archive_state: "done", archive_error: null } satisfies Partial<Post>),
          lastEventId: "1790000000500001",
        }),
      ),
    );
    expect(within(reader()).getByRole("status")).toHaveTextContent("In KB");
  });
});

describe("↻ Refresh lives in the list header", () => {
  it("the ↻ button is inside the list pane header, and nothing in the title bar says Refresh", async () => {
    setup();
    const pane = within(await screen.findByRole("region", { name: "All unread" }));
    expect(pane.getByRole("button", { name: "↻ Refresh" })).toBeInTheDocument();
    expect(pane.getByRole("button", { name: "✓ Mark all read" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "↻ Refresh" })).toHaveLength(1);
    // The app's title bar: the one header outside the panes.
    const titleBar = () => {
      const bars = screen.getAllByRole("banner").filter((b) => b.closest("section") === null);
      expect(bars).toHaveLength(1);
      return within(bars[0] as HTMLElement);
    };
    expect(titleBar().queryByText(/Refresh/)).toBeNull();
    await refresh();
    expect(await pane.findByText(/^Updated /)).toBeInTheDocument();
    expect(titleBar().queryByText(/Updated/)).toBeNull();
  });
});

describe("A reload while reading on a narrow screen (R30)", () => {
  function stubNarrow() {
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      media: "(max-width: 767px)",
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
  }
  const reconnect = () => {
    const source = stream();
    if (!source) throw new Error("no rss.post_changed stream");
    act(() => source.onopen?.());
  };

  it("when the open post drops out of the reloaded list, the narrow reader keeps it", async () => {
    stubNarrow();
    let first = true;
    setup({
      "GET /api/rss/posts": () => {
        const items = first ? [post(1), post(2)] : [post(2)];
        first = false;
        return Response.json({ items, next_before: null });
      },
    });
    await openFirst();
    reconnect();
    await waitFor(() => expect(first).toBe(false));
    await act(async () => {});
    // Read, it no longer matches All unread, but it stays open until the reader moves on.
    expect(screen.getByRole("region", { name: "Reader" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Post 1", level: 1 })).toBeInTheDocument();
  });

  it("a failed reload keeps the reader and says why", async () => {
    stubNarrow();
    let fail = false;
    setup({
      "GET /api/rss/posts": () =>
        fail
          ? Response.json({ detail: "database locked" }, { status: 500 })
          : Response.json({ items: [post(1)], next_before: null }),
    });
    await openFirst();
    fail = true;
    reconnect();
    expect((await screen.findAllByText(/Couldn't reload posts: database locked\./)).length).toBeGreaterThan(0);
    expect(screen.getByRole("region", { name: "Reader" })).toBeInTheDocument();
  });
});

describe("Error copy (§9)", () => {
  it("a request that got no answer says Jesinia didn't answer, with the fix", async () => {
    setup({ "GET /api/rss/feeds": () => Promise.reject(new TypeError("Failed to fetch")) });
    expect(
      (
        await screen.findAllByText(
          "Couldn't load feeds: Jesinia didn't answer (Failed to fetch). Check that Jesinia is running, then try again.",
        )
      ).length,
    ).toBeGreaterThan(0);
  });

  it("a server's refusal gets its own next step", async () => {
    setup({ "GET /api/rss/feeds": () => Response.json({ detail: "rss.sqlite is locked" }, { status: 503 }) });
    expect(
      (await screen.findAllByText("Couldn't load feeds: rss.sqlite is locked. Press ↻ Refresh to try again.")).length,
    ).toBeGreaterThan(0);
  });
});

describe("/ searches feed names too", () => {
  it("finds a feed by name even when no headline matches, and opens it", async () => {
    setup({ "GET /api/rss/posts/search": [] });
    await screen.findByRole("option", { name: /Post 1/ });
    // No group while nothing is typed.
    expect(screen.queryByRole("group", { name: "Matching feeds" })).toBeNull();
    await userEvent.keyboard("/");
    await userEvent.keyboard("LOOSE");
    const feeds = within(await screen.findByRole("group", { name: "Matching feeds" }));
    expect(await screen.findByText("No headlines match “LOOSE”.")).toBeInTheDocument();
    expect(feeds.getAllByRole("button").map((b) => b.textContent)).toEqual(["Loose feed"]);
    await userEvent.click(feeds.getByRole("button", { name: "Loose feed" }));
    expect(await screen.findByRole("heading", { name: "Loose feed", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search headlines" })).toHaveValue("");
    expect(screen.queryByRole("group", { name: "Matching feeds" })).toBeNull();
  });

  it("shows no feed group when no feed name matches", async () => {
    setup({ "GET /api/rss/posts/search": [post(2)] });
    await screen.findByRole("option", { name: /Post 1/ });
    await userEvent.type(screen.getByRole("searchbox", { name: "Search headlines" }), "zzz");
    expect(await screen.findByRole("heading", { name: "Results for “zzz”", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Matching feeds" })).toBeNull();
  });
});
