// SPDX-License-Identifier: AGPL-3.0-or-later
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { COLLAPSED_KEY } from "../Feeds";
import { ROW_HEIGHT } from "../PostList";
import { FEEDS, POSTS, post, routes } from "./fixtures";

function setup(over: Routes = {}) {
  const env = fakeClient(routes(over));
  const view = render(<App client={env.client} />);
  return { ...env, view };
}

const writes = (calls: Request[], path: string) =>
  calls.filter((c) => c.method !== "GET" && new URL(c.url).pathname === path);
const gets = (calls: Request[], path: string) =>
  calls.filter((c) => c.method === "GET" && new URL(c.url).pathname === path);
const press = (key: string, init: KeyboardEventInit = {}) => fireEvent.keyDown(document.body, { key, ...init });

async function list() {
  return screen.findByRole("listbox", { name: "Posts" });
}
/** A row of the current list (a new list replaces the listbox, so it is looked up each try). */
async function option(name: RegExp | string) {
  return waitFor(() => within(screen.getByRole("listbox", { name: "Posts" })).getByRole("option", { name }));
}
/** A toast's own message (the live region repeats it for screen readers). */
const toast = async (text: RegExp) =>
  (await screen.findAllByText(text)).find((el) => el.closest("[data-toast]") !== null);
const reader = () => screen.getByRole("region", { name: "Reader" });

function stubNarrow(matches: boolean) {
  const listeners = new Set<() => void>();
  const mql = {
    matches,
    media: "(max-width: 767px)",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", () => mql);
  return {
    set(next: boolean) {
      mql.matches = next;
      act(() => listeners.forEach((fn) => fn()));
    },
  };
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.stubGlobal("matchMedia", undefined); // jsdom has none: the wide layout
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("feeds pane", () => {
  it("shows the views, folders and feeds with counts capped at 999+", async () => {
    setup();
    const nav = await screen.findByRole("navigation", { name: "Feeds" });
    const unread = await within(nav).findByRole("button", { name: /All unread/ });
    await waitFor(() => expect(unread).toHaveTextContent("999+")); // counts load on their own
    expect(within(nav).getByRole("button", { name: /In KB/ })).toHaveTextContent("31");
    // A folder's count shows while it is collapsed (its feeds carry their own).
    expect(within(nav).getByRole("button", { name: /^News/ })).not.toHaveTextContent("999+");
    await userEvent.click(within(nav).getByRole("button", { name: "Feeds in News" }));
    expect(within(nav).getByRole("button", { name: /^News/ })).toHaveTextContent("999+");
    await userEvent.click(within(nav).getByRole("button", { name: "Feeds in News" }));
    expect(within(nav).getByRole("button", { name: /^Hacker News/ })).toHaveTextContent("999+");
    expect(within(nav).getByRole("button", { name: /^Fly\.io blog/ })).toHaveTextContent("4");
    expect(within(nav).getByRole("button", { name: /^Loose feed/ })).toHaveTextContent("2");
    // A feed with no unread posts shows no zero.
    expect(within(nav).getByRole("button", { name: /^Old Blog/ })).not.toHaveTextContent("0");
  });

  it("the sidebar shows no feed warnings or retry; Manage feeds counts the failing ones", async () => {
    setup();
    const nav = within(await screen.findByRole("navigation", { name: "Feeds" }));
    expect(await nav.findByRole("button", { name: "⚙ Manage feeds · 1" })).toBeInTheDocument();
    expect(nav.getByRole("button", { name: /^Old Blog/ })).not.toHaveTextContent("!");
    expect(nav.queryByRole("img", { name: /Warning/ })).toBeNull();
    expect(nav.queryByText(/Failing for 3 days/)).toBeNull();
    expect(nav.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(nav.queryByRole("button", { name: "+ Add feed" })).toBeNull();
  });

  it("Manage feeds shows no count when nothing fails", async () => {
    setup({ "GET /api/rss/feeds": FEEDS.map((f) => ({ ...f, warning: null })) });
    const nav = within(await screen.findByRole("navigation", { name: "Feeds" }));
    expect(await nav.findByRole("button", { name: "⚙ Manage feeds" })).toBeInTheDocument();
  });

  it("folders collapse, and the choice is remembered", async () => {
    const first = setup();
    const folder = await screen.findByRole("button", { name: "Feeds in Engineering" });
    expect(folder).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(folder);
    expect(folder).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /^Fly\.io blog/ })).toBeNull();
    expect(JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]")).toEqual([11]);
    first.view.unmount();
    setup();
    expect(await screen.findByRole("button", { name: "Feeds in Engineering" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await userEvent.click(screen.getByRole("button", { name: "Feeds in Engineering" }));
    expect(screen.getByRole("button", { name: /^Fly\.io blog/ })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]")).toEqual([]);
  });

  it("collapsing still works when storage throws or holds junk", async () => {
    localStorage.setItem(COLLAPSED_KEY, "{not json");
    setup();
    const folder = await screen.findByRole("button", { name: "Feeds in News" });
    expect(folder).toHaveAttribute("aria-expanded", "true");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    await userEvent.click(folder);
    expect(folder).toHaveAttribute("aria-expanded", "false");
  });

  it("choosing a feed or folder lists its posts; the filter switches unread and all", async () => {
    const { calls } = setup();
    await list();
    await userEvent.click(await screen.findByRole("button", { name: /^Hacker News/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("feed=1"));
    expect(screen.getByRole("heading", { name: "Hacker News", level: 2 })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Show" }), "all");
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toMatch(/view=all.*feed=1|feed=1.*view=all/));
    await userEvent.click(screen.getByRole("button", { name: /^News/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("folder=10"));
    await userEvent.click(screen.getByRole("button", { name: /In KB/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("view=kept"));
    expect(screen.getByRole("combobox", { name: "Show" })).toBeDisabled();
    // From In KB, a feed or folder opens on its unread posts.
    await userEvent.click(screen.getByRole("button", { name: /^Loose feed/ }));
    await waitFor(() =>
      expect(gets(calls, "/api/rss/posts").at(-1)?.url).toMatch(/view=unread.*feed=4|feed=4.*view=unread/),
    );
    await userEvent.click(screen.getByRole("button", { name: /In KB/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Engineering/ }));
    await waitFor(() =>
      expect(gets(calls, "/api/rss/posts").at(-1)?.url).toMatch(/view=unread.*folder=11|folder=11.*view=unread/),
    );
  });
});

describe("list and reader", () => {
  it("selecting a post opens it in the reader and marks it read", async () => {
    const { calls } = setup();
    expect(await screen.findByText("Choose a post to read it here.")).toBeInTheDocument();
    const row = await option(/Post 1/);
    expect(within(row).getByRole("img", { name: "Unread" })).toBeInTheDocument();
    await userEvent.click(row);
    expect(within(reader()).getByRole("heading", { name: "Post 1", level: 1 })).toBeInTheDocument();
    expect(reader()).toHaveTextContent("Body of post 1.");
    expect(reader()).toHaveTextContent(/Fly\.io blog · Ada · 14 Oct 2026 · 1 min/);
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1")).toHaveLength(1));
    const patch = writes(calls, "/api/rss/posts/1")[0];
    expect(patch?.method).toBe("PATCH");
    expect(patch?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await patch?.clone().json()).toEqual({ read: true });
    await waitFor(() => expect(within(row).queryByRole("img", { name: "Unread" })).toBeNull());
    // Selecting a post that is already read writes nothing.
    await userEvent.click(await option(/Post 3/));
    expect(writes(calls, "/api/rss/posts/3")).toHaveLength(0);
  });

  it("the reader's toolbar holds Open original, Engage and Keep; the bottom bar Previous, Next and Mark unread", async () => {
    const { calls } = setup();
    await userEvent.click(await option(/Post 2/));
    const toolbar = within(reader()).getByRole("group", { name: "Post actions" });
    const original = within(toolbar).getByRole("link", { name: "Open original ↗" });
    expect(original).toHaveAttribute("href", "https://fly.io/blog/post-2");
    expect(original).toHaveAttribute("target", "_blank");
    expect(original).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(toolbar).getByRole("button", { name: "Engage" })).toBeInTheDocument();
    expect(within(toolbar).getByRole("button", { name: "Keep" })).toBeInTheDocument();
    // The reader remounts per post, so the bar is looked up each time.
    const bottom = () => within(within(reader()).getByRole("group", { name: "Reading" }));
    await userEvent.click(bottom().getByRole("button", { name: "Next" }));
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 3");
    await userEvent.click(bottom().getByRole("button", { name: "Previous" }));
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 2");
    await userEvent.click(await bottom().findByRole("button", { name: "Mark unread" }));
    await waitFor(() => expect(writes(calls, "/api/rss/posts/2")).toHaveLength(2));
    expect(await writes(calls, "/api/rss/posts/2")[1]?.clone().json()).toEqual({ read: false });
    expect(await bottom().findByRole("button", { name: "Mark read" })).toBeInTheDocument();
    // No on-screen key guide (design pass): the keys are listed only by `?`.
    expect(screen.queryByText(/j\s*\/\s*k/)).toBeNull();
  });

  it("read posts are marked for dimming", async () => {
    setup();
    expect(await option(/Post 3/)).toHaveAttribute("data-read", "true");
    expect(await option(/Post 1/)).not.toHaveAttribute("data-read");
  });

  it("rapid read toggles end in the last state asked for, whatever order the answers come in", async () => {
    const pending: ((r: Response) => void)[] = [];
    setup({
      "PATCH /api/rss/posts/3": async (req: Request) => {
        const body = (await req.clone().json()) as { read: boolean };
        return new Promise<Response>((resolve) => pending.push(() => resolve(Response.json(post(3, body)))));
      },
    });
    await userEvent.click(await option(/Post 3/));
    press("m"); // → unread
    press("m"); // → read again
    await waitFor(() => expect(pending).toHaveLength(2));
    act(() => pending[1]?.(new Response()));
    act(() => pending[0]?.(new Response()));
    await waitFor(() => expect(within(reader()).getByRole("button", { name: "Mark unread" })).toBeInTheDocument());
    await act(async () => {}); // let both answers settle: the older one must not win
    expect(within(reader()).getByRole("button", { name: "Mark unread" })).toBeInTheDocument();
  });

  it("unread counts that fail to load say why, once", async () => {
    const { calls } = setup({
      "GET /api/rss/counts": () => Response.json({ detail: "counts busy" }, { status: 503 }),
      "POST /api/rss/refresh": { polled: 1, new_items: 0, failed: 0 },
    });
    expect(await toast(/Couldn't load unread counts: counts busy/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "↻ Refresh" }));
    await waitFor(() => expect(gets(calls, "/api/rss/counts").length).toBeGreaterThan(1));
    expect(document.querySelectorAll("[data-toast]")).toHaveLength(1);
  });

  it("an unsafe original URL gets no link and o opens nothing", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    setup({
      "GET /api/rss/posts": { items: [post(1, { url: "javascript:alert(1)", read: true })], next_before: null },
    });
    await userEvent.click(await option(/Post 1/));
    expect(within(reader()).queryByRole("link", { name: "Open original ↗" })).toBeNull();
    press("o");
    expect(open).not.toHaveBeenCalled();
  });

  it("a failed read toggle says so and leaves the post as it was", async () => {
    setup({
      "PATCH /api/rss/posts/1": () =>
        Response.json({ error: "PostNotFound", detail: "post 1 is gone" }, { status: 404 }),
    });
    await userEvent.click(await option(/Post 1/));
    expect(await toast(/Couldn't mark the post read: post 1 is gone/)).toBeInTheDocument();
    expect(within(await option(/Post 1/)).getByRole("img", { name: "Unread" })).toBeInTheDocument();
  });

  it("shows the archive status: Archiving…, In KB, or Archive incomplete with Retry", async () => {
    const { calls } = setup({
      "GET /api/rss/posts": {
        items: [
          post(1, { read: true, kept_page_id: "k1", archive_state: "archiving" }),
          post(2, { read: true, kept_page_id: "k2", archive_state: "done" }),
          post(3, { read: true, kept_page_id: "k3", archive_state: "failed", archive_error: "The site timed out." }),
        ],
        next_before: null,
      },
      "POST /api/rss/posts/3/archive": post(3, { read: true, kept_page_id: "k3", archive_state: "archiving" }),
    });
    expect(within(await option(/Post 1/)).getByText("Archiving…")).toBeInTheDocument();
    expect(within(await option(/Post 2/)).getByText("In KB")).toBeInTheDocument();
    expect(within(await option(/Post 3/)).getByText("Archive incomplete")).toBeInTheDocument();
    await userEvent.click(await option(/Post 3/));
    const status = within(reader()).getByRole("status");
    expect(status).toHaveTextContent("Archive incomplete: The site timed out.");
    await userEvent.click(within(status).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(writes(calls, "/api/rss/posts/3/archive")).toHaveLength(1));
    expect(await within(reader()).findByText("Archiving…")).toBeInTheDocument();
  });

  it("a failed archive retry shows its cause", async () => {
    setup({
      "GET /api/rss/posts": {
        items: [post(1, { read: true, kept_page_id: "k", archive_state: "failed", archive_error: null })],
        next_before: null,
      },
      "POST /api/rss/posts/1/archive": () =>
        Response.json({ error: "ArchiveNotFailed", detail: "already running" }, { status: 409 }),
    });
    await userEvent.click(await option(/Post 1/));
    await userEvent.click(within(reader()).getByRole("button", { name: "Retry" }));
    expect(await within(reader()).findByText(/Couldn't retry the archive: already running/)).toBeInTheDocument();
  });

  it("shows empty and failed lists with the cause and a retry", async () => {
    let fail = true;
    setup({
      "GET /api/rss/posts": () =>
        fail
          ? Response.json({ error: "InvalidCursor", detail: "database locked" }, { status: 500 })
          : Response.json({ items: [], next_before: null }),
    });
    expect(await screen.findByText(/Couldn't load posts: database locked/)).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Nothing unread here.")).toBeInTheDocument();
  });

  it("the list is windowed: only the rows in view are in the DOM", async () => {
    const many = Array.from({ length: 500 }, (_, i) => post(i + 1, { read: true }));
    setup({ "GET /api/rss/posts": { items: many, next_before: null } });
    const box = await list();
    await within(box).findByRole("option", { name: /Post 1\b/ });
    expect(within(box).getAllByRole("option").length).toBeLessThan(40);
    expect(within(box).queryByRole("option", { name: /Post 300\b/ })).toBeNull();
    const scroller = screen.getByTestId("post-scroller");
    scroller.scrollTop = ROW_HEIGHT * 299;
    fireEvent.scroll(scroller);
    expect(await within(box).findByRole("option", { name: /Post 300\b/ })).toBeInTheDocument();
    expect(within(box).queryByRole("option", { name: /Post 1\b/ })).toBeNull();
    expect(within(box).getAllByRole("option")[0]).toHaveAttribute("aria-posinset");
    expect(within(box).getAllByRole("option")[0]).toHaveAttribute("aria-setsize", "500");
  });

  it("a fast scope switch clears the old list: no stale read-above or paging, and the new list starts at the top", async () => {
    const waiting = new Map<string, (r: Response) => void>();
    const many = Array.from({ length: 500 }, (_, i) => post(i + 1));
    const { calls } = setup({
      "GET /api/rss/posts": (req: Request) => {
        const feed = new URL(req.url).searchParams.get("feed");
        if (feed === null) return Response.json({ items: many, next_before: "old-cursor" });
        return new Promise<Response>((resolve) => waiting.set(feed, resolve));
      },
    });
    await option(/Post 1\b/);
    const scroller = screen.getByTestId("post-scroller");
    scroller.scrollTop = ROW_HEIGHT * 200;
    fireEvent.scroll(scroller);
    await userEvent.click(await option(/Post 201\b/));
    await userEvent.click(screen.getByRole("button", { name: /^Hacker News/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Fly\.io blog/ }));
    expect(screen.queryByRole("listbox", { name: "Posts" })).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    press("A", { shiftKey: true });
    press("j");
    await waitFor(() => expect(waiting.size).toBe(2));
    act(() => waiting.get("2")?.(Response.json({ items: [post(900, { title: "Fresh" })], next_before: null })));
    act(() => waiting.get("1")?.(Response.json({ items: [post(800, { title: "Stale" })], next_before: null })));
    expect(await option(/Fresh/)).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Stale/ })).toBeNull();
    expect(screen.getByTestId("post-scroller").scrollTop).toBe(0);
    expect(writes(calls, "/api/rss/posts/read-above")).toHaveLength(0);
    expect(gets(calls, "/api/rss/posts").filter((c) => new URL(c.url).searchParams.has("before"))).toHaveLength(0);
  });

  it("loads the next page when the end comes into view", async () => {
    const { calls } = setup({
      "GET /api/rss/posts": (req: Request) =>
        new URL(req.url).searchParams.get("before") === "c1"
          ? Response.json({ items: [post(9)], next_before: null })
          : Response.json({ items: [post(1), post(2)], next_before: "c1" }),
    });
    expect(await option(/Post 9/)).toBeInTheDocument();
    expect(gets(calls, "/api/rss/posts")).toHaveLength(2);
  });

  it("a failed next page stops paging and says why", async () => {
    const { calls } = setup({
      "GET /api/rss/posts": (req: Request) =>
        new URL(req.url).searchParams.get("before")
          ? Response.json({ detail: "cursor expired" }, { status: 400 })
          : Response.json({ items: [post(1)], next_before: "c1" }),
    });
    expect(await toast(/Couldn't load more posts: cursor expired/)).toBeInTheDocument();
    expect(gets(calls, "/api/rss/posts")).toHaveLength(2);
    expect(await option(/Post 1/)).toBeInTheDocument();
  });

  it("a post with no content, author or date shows its snippet and what it has", async () => {
    setup({
      "GET /api/rss/posts": {
        items: [post(1, { read: true, content_html: null, author: null, published_at: null })],
        next_before: null,
      },
    });
    await userEvent.click(await option(/Post 1/));
    expect(reader()).toHaveTextContent("Snippet 1");
    expect(reader()).toHaveTextContent("Fly.io blog · 14 Oct 2026 · 1 min");
  });

  it("feeds that fail to load say why", async () => {
    setup({ "GET /api/rss/feeds": () => Response.json({ detail: "rss.sqlite is locked" }, { status: 503 }) });
    expect(await toast(/Couldn't load feeds: rss\.sqlite is locked/)).toBeInTheDocument();
  });

  it("Refresh polls the feeds, then reloads the list and counts", async () => {
    const { calls } = setup({ "POST /api/rss/refresh": { polled: 4, new_items: 2, failed: 0 } });
    await list();
    const before = gets(calls, "/api/rss/counts").length;
    await userEvent.click(screen.getByRole("button", { name: "↻ Refresh" }));
    await waitFor(() => expect(writes(calls, "/api/rss/refresh")).toHaveLength(1));
    expect(writes(calls, "/api/rss/refresh")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    await waitFor(() => expect(gets(calls, "/api/rss/counts").length).toBeGreaterThan(before));
    expect(await screen.findByText(/Updated/)).toBeInTheDocument();
  });

  it("a failed refresh shows its cause", async () => {
    setup({ "POST /api/rss/refresh": () => Response.json({ detail: "poller stopped" }, { status: 500 }) });
    await list();
    await userEvent.click(screen.getByRole("button", { name: "↻ Refresh" }));
    expect(await toast(/Couldn't refresh: poller stopped/)).toBeInTheDocument();
  });
});

describe("keys", () => {
  it("j and k move the selection, and the reader follows", async () => {
    const { calls } = setup();
    await option(/Post 1/);
    press("k"); // nothing selected yet: nothing happens
    expect(screen.getByText("Choose a post to read it here.")).toBeInTheDocument();
    press("j");
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
    press("j");
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 2");
    press("k");
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
    press("k"); // at the top: stays
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
    await waitFor(() => expect(writes(calls, "/api/rss/posts/2")).toHaveLength(1));
    expect(within(await option(/Post 1/))).toBeTruthy();
    expect(await option(/Post 1/)).toHaveAttribute("aria-selected", "true");
  });

  it("m toggles read, Enter and o open the original", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { calls } = setup();
    press("m"); // nothing selected
    press("Enter");
    expect(open).not.toHaveBeenCalled();
    await userEvent.click(await option(/Post 1/));
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1")).toHaveLength(1));
    await waitFor(() => expect(within(reader()).getByRole("button", { name: "Mark unread" })).toBeEnabled());
    press("m");
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1")).toHaveLength(2));
    expect(await writes(calls, "/api/rss/posts/1")[1]?.clone().json()).toEqual({ read: false });
    (document.activeElement as HTMLElement | null)?.blur();
    press("Enter");
    press("o");
    expect(open).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenCalledWith("https://fly.io/blog/post-1", "_blank", "noopener,noreferrer");
  });

  it("Shift+A marks everything above the selection read, for the current view, as of the load", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-16T10:00:00.000Z"));
    const { calls } = setup();
    await option(/Post 2/);
    vi.setSystemTime(new Date("2026-10-16T10:05:00.000Z"));
    press("A", { shiftKey: true }); // nothing selected: nothing sent
    await userEvent.click(await option(/Post 2/));
    press("A", { shiftKey: true });
    await waitFor(() => expect(writes(calls, "/api/rss/posts/read-above")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/read-above")[0]?.clone().json()).toEqual({
      post_id: 2,
      view: "unread",
      feed: null,
      folder: null,
      as_of: "2026-10-16T10:00:00.000Z",
    });
    expect(writes(calls, "/api/rss/posts/read-above")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    await waitFor(() =>
      expect(within(screen.getByRole("option", { name: /Post 1/ })).queryByRole("img", { name: "Unread" })).toBeNull(),
    );
    expect(
      within(screen.getByRole("option", { name: /Post 4/ })).getByRole("img", { name: "Unread" }),
    ).toBeInTheDocument();
  });

  it("Shift+A in a feed sends that feed, and a failure says so", async () => {
    const { calls } = setup({
      "POST /api/rss/posts/read-above": () => Response.json({ detail: "bad time" }, { status: 400 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /^Fly\.io blog/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("feed=2"));
    await userEvent.click(await option(/Post 1/));
    press("A", { shiftKey: true });
    await waitFor(() => expect(writes(calls, "/api/rss/posts/read-above")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/read-above")[0]?.clone().json()).toMatchObject({
      view: "unread",
      feed: 2,
      folder: null,
    });
    expect(await toast(/Couldn't mark these read: bad time/)).toBeInTheDocument();
  });

  it("g a and g i go to All unread and In KB", async () => {
    const { calls } = setup();
    await list();
    press("g");
    press("i");
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("view=kept"));
    expect(screen.getByRole("heading", { name: "In KB", level: 2 })).toBeInTheDocument();
    press("g");
    press("a");
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("view=unread"));
    expect(screen.getByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
  });

  it("/ focuses search, which lists matching headlines", async () => {
    const { calls } = setup({ "GET /api/rss/posts/search": [post(7, { title: "SQLite WAL" })] });
    await list();
    press("/");
    const box = screen.getByRole("searchbox", { name: "Search headlines" });
    expect(box).toHaveFocus();
    await userEvent.type(box, "sqlite");
    expect(await option(/SQLite WAL/)).toBeInTheDocument();
    expect(gets(calls, "/api/rss/posts/search").at(-1)?.url).toContain("q=sqlite");
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Results for “sqlite”");
    await userEvent.clear(box);
    expect(await option(/Post 1/)).toBeInTheDocument();
  });

  it("a failed search shows the cause", async () => {
    setup({ "GET /api/rss/posts/search": () => Response.json({ detail: "index busy" }, { status: 500 }) });
    await list();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search headlines" }), "x");
    expect(await screen.findByText(/Couldn't load posts: index busy/)).toBeInTheDocument();
  });

  it("? lists the keys, and Esc or Close hides them", async () => {
    setup();
    await list();
    press("?", { shiftKey: true });
    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    press("j"); // keys wait while the list is open
    expect(screen.getByText("Choose a post to read it here.")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Next post");
    expect(dialog).toHaveTextContent("Mark everything above read");
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    press("?", { shiftKey: true });
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("focus", () => {
  it("the key list keeps Tab inside and returns focus to where it was", async () => {
    setup();
    const box = await list();
    box.focus();
    press("?", { shiftKey: true });
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();
    await userEvent.tab();
    expect(close).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(close).toHaveFocus();
    await userEvent.click(close);
    expect(box).toHaveFocus();
  });

  it("arrow keys move the selection while the list has focus", async () => {
    setup();
    const box = await list();
    box.focus();
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
    fireEvent.keyDown(box, { key: "x" });
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
  });
});

describe("mark all read", () => {
  const MARKED = {
    "POST /api/rss/posts/mark-read": { marked: 2, ids: [1, 2] },
    "POST /api/rss/posts/read-state": { marked: 2 },
  };
  const markAll = () => screen.getByRole("button", { name: "✓ Mark all read" });

  it("mark all read in the header applies to the folder in view and offers undo", async () => {
    const { calls } = setup(MARKED);
    await list();
    await userEvent.click(screen.getByRole("button", { name: /^Engineering/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("folder=11"));
    await option(/Post 1/);
    await userEvent.click(markAll());
    await waitFor(() => expect(writes(calls, "/api/rss/posts/mark-read")).toHaveLength(1));
    const sentBody = (await writes(calls, "/api/rss/posts/mark-read")[0]?.clone().json()) as Record<string, unknown>;
    expect(sentBody).toMatchObject({ feed: null, folder: 11 });
    expect(typeof sentBody.as_of).toBe("string");
    expect(writes(calls, "/api/rss/posts/mark-read")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    const message = await toast(/^Marked 2 in Engineering read$/);
    const shown = message?.closest("[data-toast]") as HTMLElement;
    await userEvent.click(within(shown).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(writes(calls, "/api/rss/posts/read-state")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/read-state")[0]?.clone().json()).toEqual({ ids: [1, 2], read: false });
  });

  it("a failed Undo says why and what to do instead", async () => {
    setup({
      ...MARKED,
      "POST /api/rss/posts/read-state": () => Response.json({ detail: "rss.sqlite is locked" }, { status: 503 }),
    });
    await option(/Post 1/);
    await userEvent.click(markAll());
    const message = await toast(/^Marked 2 in All unread read$/);
    await userEvent.click(
      within(message?.closest("[data-toast]") as HTMLElement).getByRole("button", { name: "Undo" }),
    );
    expect(
      await toast(/^Couldn't undo marking All unread read: rss\.sqlite is locked\. Mark the posts unread with m\.$/),
    ).toBeInTheDocument();
  });

  it("mark all read at All unread marks everything", async () => {
    const { calls } = setup(MARKED);
    await option(/Post 1/);
    await userEvent.click(markAll());
    await waitFor(() => expect(writes(calls, "/api/rss/posts/mark-read")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/mark-read")[0]?.clone().json()).toMatchObject({
      feed: null,
      folder: null,
    });
    expect(await toast(/^Marked 2 in All unread read$/)).toBeInTheDocument();
  });

  it("with nothing to mark it says so, with no Undo", async () => {
    setup({ "POST /api/rss/posts/mark-read": { marked: 0, ids: [] } });
    await option(/Post 1/);
    await userEvent.click(markAll());
    const message = await toast(/^Nothing unread in All unread$/);
    expect(within(message?.closest("[data-toast]") as HTMLElement).queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("a failed mark all read puts the rows back and says why", async () => {
    setup({
      "POST /api/rss/posts/mark-read": () => Response.json({ detail: "rss.sqlite is locked" }, { status: 503 }),
    });
    await option(/Post 1/);
    await userEvent.click(markAll());
    expect(await toast(/^Couldn't mark All unread read: rss\.sqlite is locked\./)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("option", { name: /Post 1/ })).not.toHaveAttribute("data-read"));
    expect(screen.getByRole("option", { name: /Post 3/ })).toHaveAttribute("data-read", "true");
  });

  it("sidebar mark read on a feed does not change the scope", async () => {
    const { calls } = setup({ ...MARKED, "POST /api/rss/posts/mark-read": { marked: 3, ids: [1, 2, 4] } });
    await option(/Post 1/);
    const nav = within(screen.getByRole("navigation", { name: "Feeds" }));
    await waitFor(() => expect(gets(calls, "/api/rss/counts").length).toBeGreaterThan(0));
    const counts = gets(calls, "/api/rss/counts").length;
    const posts = gets(calls, "/api/rss/posts").length;
    await userEvent.hover(nav.getByRole("button", { name: /^Fly\.io blog/ }));
    const mark = nav.getByRole("button", { name: "Mark Fly.io blog read" });
    expect(mark).toHaveTextContent("Mark read");
    await userEvent.click(mark);
    await waitFor(() => expect(writes(calls, "/api/rss/posts/mark-read")).toHaveLength(1));
    const body = (await writes(calls, "/api/rss/posts/mark-read")[0]?.clone().json()) as Record<string, unknown>;
    expect(body).toMatchObject({ feed: 2, folder: null });
    expect(typeof body.as_of).toBe("string");
    expect(await toast(/^Marked 3 in Fly\.io blog read$/)).toBeInTheDocument();
    // Its rows on screen read at once, without reloading the list.
    expect(screen.getByRole("option", { name: /Post 1/ })).toHaveAttribute("data-read", "true");
    expect(screen.getByRole("option", { name: /Post 4/ })).toHaveAttribute("data-read", "true");
    await waitFor(() => expect(gets(calls, "/api/rss/counts").length).toBeGreaterThan(counts));
    expect(screen.getByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    expect(nav.getByRole("button", { name: /^All unread/ })).toHaveAttribute("aria-current", "true");
    expect(nav.getByRole("button", { name: /^Fly\.io blog/ })).not.toHaveAttribute("aria-current");
    // One feed doesn't cover All unread: the list is not reloaded.
    expect(gets(calls, "/api/rss/posts")).toHaveLength(posts);
  });

  it("sidebar mark read on the folder in view reloads the list", async () => {
    let marked = false;
    const { calls } = setup({
      "GET /api/rss/posts": () =>
        Response.json({ items: marked ? POSTS.map((p) => ({ ...p, read: true })) : POSTS, next_before: null }),
      "POST /api/rss/posts/mark-read": () => {
        marked = true;
        return Response.json({ marked: 3, ids: [1, 2, 4] });
      },
    });
    await list();
    const nav = within(screen.getByRole("navigation", { name: "Feeds" }));
    await userEvent.click(await nav.findByRole("button", { name: /^Fly\.io blog/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("feed=2"));
    await option(/Post 1/);
    const before = gets(calls, "/api/rss/posts").length;
    await userEvent.click(nav.getByRole("button", { name: "Mark Engineering read" }));
    await waitFor(() => expect(writes(calls, "/api/rss/posts/mark-read")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/mark-read")[0]?.clone().json()).toMatchObject({
      feed: null,
      folder: 11,
    });
    await waitFor(() => expect(gets(calls, "/api/rss/posts").length).toBeGreaterThan(before));
    expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("feed=2");
    await waitFor(() => expect(screen.getByRole("option", { name: /Post 1/ })).toHaveAttribute("data-read", "true"));
    expect(screen.getByRole("option", { name: /Post 4/ })).toHaveAttribute("data-read", "true");
    expect(screen.getByRole("heading", { name: "Fly.io blog", level: 2 })).toBeInTheDocument();
  });

  it("the sidebar Mark read is reachable by Tab and its click never selects the row", async () => {
    const { calls } = setup(MARKED);
    await option(/Post 1/);
    const nav = within(screen.getByRole("navigation", { name: "Feeds" }));
    const row = nav.getByRole("button", { name: /^Loose feed/ });
    row.focus();
    await userEvent.tab();
    expect(nav.getByRole("button", { name: "Mark Loose feed read" })).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(writes(calls, "/api/rss/posts/mark-read")).toHaveLength(1));
    expect(row).not.toHaveAttribute("aria-current");
    expect(gets(calls, "/api/rss/posts").every((c) => !c.url.includes("feed=4"))).toBe(true);
  });

  it("no mark all read in In KB or while searching", async () => {
    setup({ ...MARKED, "GET /api/rss/posts/search": [post(2)] });
    await option(/Post 1/);
    expect(markAll()).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^In KB/ }));
    expect(await screen.findByRole("heading", { name: "In KB", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "✓ Mark all read" })).toBeNull();
    press("M", { shiftKey: true });
    await userEvent.click(screen.getByRole("button", { name: /^All unread/ }));
    await userEvent.type(screen.getByRole("searchbox", { name: "Search headlines" }), "two");
    expect(await screen.findByRole("heading", { name: "Results for “two”", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "✓ Mark all read" })).toBeNull();
    (document.activeElement as HTMLElement).blur();
    press("M", { shiftKey: true });
    await act(async () => {});
    expect(screen.queryAllByText(/^Marked /)).toHaveLength(0);
  });

  it("Shift+M marks all read here, as of the list's load", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-16T10:00:00.000Z"));
    const { calls } = setup(MARKED);
    await userEvent.click(await screen.findByRole("button", { name: /^Fly\.io blog/ }));
    await waitFor(() => expect(gets(calls, "/api/rss/posts").at(-1)?.url).toContain("feed=2"));
    await option(/Post 1/);
    vi.setSystemTime(new Date("2026-10-16T10:05:00.000Z"));
    press("M", { shiftKey: true });
    await waitFor(() => expect(writes(calls, "/api/rss/posts/mark-read")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/mark-read")[0]?.clone().json()).toEqual({
      feed: 2,
      folder: null,
      as_of: "2026-10-16T10:00:00.000Z",
    });
    expect(await toast(/^Marked 2 in Fly\.io blog read$/)).toBeInTheDocument();
  });
});

describe("narrow layout", () => {
  it("the list fills the screen, a post opens the reader full-screen, and Back returns", async () => {
    stubNarrow(true);
    setup();
    await list();
    expect(screen.queryByRole("navigation", { name: "Feeds" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Reader" })).toBeNull();
    await userEvent.click(await option(/Post 1/));
    expect(within(reader()).getByRole("heading", { level: 1 })).toHaveTextContent("Post 1");
    expect(screen.queryByRole("listbox", { name: "Posts" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "‹ All unread" }));
    expect(await list()).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Reader" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "‹ Feeds" }));
    const nav = screen.getByRole("navigation", { name: "Feeds" });
    expect(screen.queryByRole("listbox", { name: "Posts" })).toBeNull();
    await userEvent.click(within(nav).getByRole("button", { name: /In KB/ }));
    expect(await list()).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "In KB", level: 2 })).toBeInTheDocument();
  });

  it("a list that fails to load (not a reload) shows its error with Try again", async () => {
    stubNarrow(true);
    setup({ "GET /api/rss/posts": () => Response.json({ detail: "database locked" }, { status: 500 }) });
    expect(await screen.findByText(/Couldn't load posts: database locked/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("j opens the reader on a narrow screen, and widening shows all three panes", async () => {
    const mq = stubNarrow(true);
    setup();
    await option(/Post 1/);
    press("j");
    expect(reader()).toBeInTheDocument();
    mq.set(false);
    expect(screen.getByRole("navigation", { name: "Feeds" })).toBeInTheDocument();
    expect(screen.getByRole("listbox", { name: "Posts" })).toBeInTheDocument();
    expect(reader()).toBeInTheDocument();
  });
});
