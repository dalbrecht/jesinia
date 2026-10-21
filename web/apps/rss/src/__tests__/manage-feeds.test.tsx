// SPDX-License-Identifier: AGPL-3.0-or-later
// Feed management at /rss/manage (spec §4.1, §7): a full screen in place of the list and the
// reader, with each feed's sync health, folder, retention, Sync now and Remove (with Undo), and
// the add form, Sync all, OPML and the default retention above.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Feed } from "../api";
import { FEEDS, routes } from "./fixtures";

const DAY = 86_400_000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

const ADDED: Feed = {
  id: 5,
  title: "Example Blog",
  url: "https://example.com/feed.xml",
  folder: "Reading",
  folder_id: 12,
  unread: 3,
  last_ok_at: null,
  fail_count: 0,
  last_error: null,
  newest_post_at: null,
  retention: null,
  warning: null,
};

/** Old Blog fails, with its count and cause. */
const FAILING: Feed[] = FEEDS.map((f) =>
  f.id === 3 ? { ...f, fail_count: 6, last_error: "HTTP 404: the feed address no longer exists" } : f,
);

function setup(over: Routes = {}) {
  const env = fakeClient(routes({ "GET /api/rss/feeds": FAILING, ...over }));
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const toast = async (text: string | RegExp) =>
  (await screen.findAllByText(text)).find((el) => el.closest("[data-toast]") !== null);
const screenRegion = () => screen.getByRole("region", { name: "Manage feeds" });

/** One input event, like a paste (typing re-renders the whole app per key). */
async function enter(field: HTMLElement, text: string): Promise<void> {
  await userEvent.click(field);
  await userEvent.paste(text);
}

/** The screen once the feeds have loaded: its rows exist only then. */
async function openManage() {
  await userEvent.click(await screen.findByRole("button", { name: /^⚙ Manage feeds/ }));
  const manage = within(screenRegion());
  await manage.findByRole("row", { name: "Old Blog" });
  return manage;
}

/** Close every toast on screen. */
async function dismissAll(): Promise<void> {
  for (const b of screen.queryAllByRole("button", { name: "Dismiss" })) await userEvent.click(b);
}

const rowOf = (manage: ReturnType<typeof within>, name: string) => within(manage.getByRole("row", { name }));

afterEach(() => vi.useRealTimers());

describe("The manage screen", () => {
  it("opens at /rss/manage from the sidebar and Back returns", async () => {
    setup();
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    await openManage();
    expect(window.location.pathname).toMatch(/\/manage$/);
    expect(screen.queryByRole("heading", { name: "All unread", level: 2 })).toBeNull();
    expect(screen.getByRole("heading", { name: "Manage feeds", level: 2 })).toHaveFocus();
    // Keys belong to the screen's fields, not the reader.
    fireEvent.keyDown(document.body, { key: "j" });
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "‹ Back to reading" }));
    expect(window.location.pathname).not.toMatch(/\/manage$/);
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Manage feeds" })).toBeNull();

    // Browser Back (popstate) returns too.
    await openManage();
    act(() => {
      window.history.pushState(null, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Manage feeds" })).toBeNull();
    // And Forward opens it again.
    act(() => {
      window.history.pushState(null, "", "/manage");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("region", { name: "Manage feeds" })).toBeInTheDocument();
  });

  it("Back to reading steps back to the reader's entry when the screen was opened here", async () => {
    setup();
    await screen.findByRole("heading", { name: "All unread", level: 2 });
    const depth = window.history.length;
    await openManage();
    expect(window.history.length).toBe(depth + 1);
    await userEvent.click(screen.getByRole("button", { name: "‹ Back to reading" }));
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    // It went back rather than adding an entry: the browser's Forward reopens the screen, and
    // its Back from the reader would not land on /manage again.
    expect(window.history.length).toBe(depth + 1);
    act(() => window.history.forward());
    expect(await screen.findByRole("region", { name: "Manage feeds" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/manage");
  });

  it("Back to reading pushes the reader when /rss/manage was loaded directly", async () => {
    window.history.replaceState(null, "", "/manage");
    setup();
    await within(await screen.findByRole("region", { name: "Manage feeds" })).findByRole("row", { name: "Old Blog" });
    const depth = window.history.length;
    await userEvent.click(screen.getByRole("button", { name: "‹ Back to reading" }));
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(window.history.length).toBe(depth + 1);
  });

  it("opens straight onto the screen when the address is /rss/manage", async () => {
    window.history.replaceState(null, "", "/manage");
    setup();
    expect(await screen.findByRole("region", { name: "Manage feeds" })).toBeInTheDocument();
    expect(await within(screenRegion()).findByRole("row", { name: "Old Blog" })).toBeInTheDocument();
  });

  it("failing feeds sort first and the Failing filter shows only them", async () => {
    setup({
      "GET /api/rss/feeds": [...FAILING, { ...ADDED, id: 6, title: "apple notes", folder: null, folder_id: null }],
    });
    const manage = await openManage();
    const names = () =>
      manage
        .getAllByRole("row")
        .slice(1)
        .map((r) => r.getAttribute("aria-label"));
    // Failing first, then by title whatever its case.
    expect(names()).toEqual(["Old Blog", "apple notes", "Fly.io blog", "Hacker News", "Loose feed"]);
    const old = rowOf(manage, "Old Blog");
    expect(old.getByText("Failing (6 tries)")).toBeInTheDocument();
    expect(old.getByText("HTTP 404: the feed address no longer exists")).toBeInTheDocument();
    expect(rowOf(manage, "Fly.io blog").getByText("OK")).toBeInTheDocument();

    const all = manage.getByRole("button", { name: "All 5" });
    const failing = manage.getByRole("button", { name: "Failing 1" });
    expect(all).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(failing);
    expect(failing).toHaveAttribute("aria-pressed", "true");
    expect(names()).toEqual(["Old Blog"]);
    await userEvent.click(all);
    expect(names()).toHaveLength(5);
  });

  it("says Failing (1 try), and shows the warning when no last error was kept", async () => {
    setup({
      "GET /api/rss/feeds": FEEDS.map((f) => (f.id === 3 ? { ...f, fail_count: 1 } : f)),
    });
    const old = rowOf(await openManage(), "Old Blog");
    expect(old.getByText("Failing (1 try)")).toBeInTheDocument();
    expect(old.getByText("Failing for 3 days: 404 from old.example")).toBeInTheDocument();
  });

  it("shows newest post and last synced", async () => {
    const synced = ago(5 * 60_000);
    setup({
      "GET /api/rss/feeds": FAILING.map((f) =>
        f.id === 2
          ? { ...f, last_ok_at: synced, newest_post_at: ago(21 * DAY + 3_600_000) }
          : f.id === 1
            ? { ...f, last_ok_at: ago(2 * DAY), newest_post_at: ago(90 * DAY) }
            : f,
      ),
    });
    const manage = await openManage();
    const fly = rowOf(manage, "Fly.io blog");
    expect(fly.getByText("3 weeks ago")).toBeInTheDocument();
    const last = fly.getByText("5 minutes ago");
    expect(last).toHaveAttribute("title", new Date(synced).toLocaleString());
    const hn = rowOf(manage, "Hacker News");
    expect(hn.getByText("2 days ago")).toBeInTheDocument();
    expect(hn.getByText("3 months ago")).toBeInTheDocument();
    const loose = rowOf(manage, "Loose feed");
    expect(loose.getByText("Never")).toBeInTheDocument();
    expect(loose.getByText("—")).toBeInTheDocument();
  });
});

describe("Adding and filing feeds", () => {
  it("adds a feed into a folder", async () => {
    let feeds = FAILING;
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "POST /api/rss/feeds": () => {
        feeds = [...FAILING, ADDED];
        return Response.json(ADDED, { status: 201 });
      },
    });
    const manage = await openManage();
    const options = [...document.querySelectorAll("#rss-folder-names option")].map((o) => o.getAttribute("value"));
    expect(options).toEqual(["Engineering", "News"]);
    const add = manage.getByRole("button", { name: "+ Add feed" });
    expect(add).toBeDisabled();
    await enter(manage.getByLabelText("Feed or site URL"), " https://example.com ");
    await enter(manage.getByLabelText("Folder (optional)"), "Reading");
    await userEvent.click(add);
    await waitFor(() => expect(sent(calls, "POST", "/api/rss/feeds")).toHaveLength(1));
    const req = sent(calls, "POST", "/api/rss/feeds")[0];
    expect(req?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await req?.json()).toEqual({ url: "https://example.com", folder: "Reading" });
    expect(await toast("Added Example Blog.")).toBeDefined();
    expect(await manage.findByRole("row", { name: "Example Blog" })).toBeInTheDocument();
    expect(manage.getByLabelText("Feed or site URL")).toHaveValue("");
    expect(screenRegion()).toBeInTheDocument(); // stays on the screen
  });

  it("says why a feed could not be added, and sends no folder when none is given", async () => {
    const { calls } = setup({
      "POST /api/rss/feeds": () =>
        Response.json(
          { error: "FeedUnavailable", detail: "no feed found at https://plain.example/: the page links to none" },
          { status: 502 },
        ),
    });
    const manage = await openManage();
    await enter(manage.getByLabelText("Feed or site URL"), "https://plain.example/");
    await userEvent.keyboard("{Enter}");
    expect(
      await manage.findByText(
        "Couldn't add the feed: no feed found at https://plain.example/: the page links to none. Check the address and try again.",
      ),
    ).toBeInTheDocument();
    expect(await sent(calls, "POST", "/api/rss/feeds")[0]?.json()).toEqual({
      url: "https://plain.example/",
      folder: null,
    });
  });

  it("moves a feed by changing its folder", async () => {
    const { calls } = setup({
      "PATCH /api/rss/feeds/2": async (req: Request) =>
        Response.json({ ...FEEDS[1], ...((await req.clone().json()) as object) }),
    });
    const manage = await openManage();
    const folder = manage.getByRole("combobox", { name: "Folder for Fly.io blog" });
    expect(folder).toHaveValue("Engineering");
    await userEvent.selectOptions(folder, "News");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(1));
    const move = sent(calls, "PATCH", "/api/rss/feeds/2")[0];
    expect(move?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await move?.json()).toEqual({ folder: "News" });
    await userEvent.selectOptions(folder, "No folder");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(2));
    expect(await sent(calls, "PATCH", "/api/rss/feeds/2")[1]?.json()).toEqual({ folder: null });

    // Into a new folder, named inline; Esc and Cancel leave it.
    await userEvent.selectOptions(folder, "New folder…");
    const name = manage.getByLabelText("New folder for Fly.io blog");
    expect(name).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(manage.queryByLabelText("New folder for Fly.io blog")).toBeNull();
    expect(folder).toHaveFocus();
    await userEvent.selectOptions(folder, "New folder…");
    await userEvent.click(manage.getByRole("button", { name: "Cancel" }));
    expect(manage.queryByLabelText("New folder for Fly.io blog")).toBeNull();
    await userEvent.selectOptions(folder, "New folder…");
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "  {Enter}");
    expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(2); // no name, nothing sent
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "Reading{Enter}");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(3));
    expect(await sent(calls, "PATCH", "/api/rss/feeds/2")[2]?.json()).toEqual({ folder: "Reading" });
    expect(await toast("Moved Fly.io blog to Reading.")).toBeDefined();
    await waitFor(() => expect(manage.queryByLabelText("New folder for Fly.io blog")).toBeNull());
  });

  it("says why a feed couldn't move, and keeps a new folder's name", async () => {
    setup({
      "PATCH /api/rss/feeds/2": () =>
        Response.json(
          { error: "InvalidFolderName", detail: "a folder name must be 1 to 100 characters" },
          { status: 400 },
        ),
    });
    const manage = await openManage();
    const folder = manage.getByRole("combobox", { name: "Folder for Fly.io blog" });
    await userEvent.selectOptions(folder, "News");
    expect(
      await toast(
        "Couldn't move Fly.io blog: a folder name must be 1 to 100 characters. Try again, or choose another folder.",
      ),
    ).toBeDefined();
    await userEvent.selectOptions(folder, "New folder…");
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "Reading{Enter}");
    expect(
      await manage.findByText(
        "Couldn't move Fly.io blog: a folder name must be 1 to 100 characters. Try again, or choose another folder name.",
      ),
    ).toBeInTheDocument();
    expect(manage.getByLabelText("New folder for Fly.io blog")).toHaveValue("Reading");
  });

  it("renames a folder from the folder picker, and reports a clash", async () => {
    let answer = Response.json({ id: 11, name: "Eng" });
    const { calls } = setup({ "PATCH /api/rss/folders/11": () => answer });
    const manage = await openManage();
    // A feed in no folder has no folder to rename.
    const loose = manage.getByRole("combobox", { name: "Folder for Loose feed" });
    expect(within(loose).queryByRole("option", { name: "Rename folder…" })).toBeNull();
    const pick = manage.getByRole("combobox", { name: "Folder for Fly.io blog" });
    await userEvent.selectOptions(pick, "Rename folder…");
    const name = manage.getByLabelText("New name for Engineering");
    expect(name).toHaveFocus();
    expect(name).toHaveValue("Engineering");
    expect(pick).toHaveValue("Engineering");
    await userEvent.clear(name);
    await userEvent.type(name, "Eng{Enter}");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/folders/11")).toHaveLength(1));
    const rename = sent(calls, "PATCH", "/api/rss/folders/11")[0];
    expect(rename?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await rename?.json()).toEqual({ name: "Eng" });
    expect(await toast("Renamed Engineering to Eng.")).toBeDefined();
    expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(0); // the feed didn't move

    answer = Response.json({ error: "FolderExists", detail: "a folder named News already exists" }, { status: 409 });
    await userEvent.selectOptions(pick, "Rename folder…");
    await userEvent.clear(manage.getByLabelText("New name for Engineering"));
    await userEvent.type(manage.getByLabelText("New name for Engineering"), "News");
    await userEvent.click(manage.getByRole("button", { name: "Save name" }));
    expect(
      await manage.findByText(
        "Couldn't rename the folder: a folder named News already exists. Try again, or choose another folder name.",
      ),
    ).toBeInTheDocument();
  });
});

describe("Sync", () => {
  it("sync now shows Syncing then the fresh status", async () => {
    let feeds = FAILING;
    let answer: (r: Response) => void = () => {};
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "POST /api/rss/feeds/3/refresh": () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    const manage = await openManage();
    const old = () => rowOf(manage, "Old Blog");
    await userEvent.click(old().getByRole("button", { name: "Sync now" }));
    expect(await old().findByText("Syncing")).toBeInTheDocument();
    expect(old().queryByText("Failing (6 tries)")).toBeNull();
    expect(sent(calls, "POST", "/api/rss/feeds/3/refresh")[0]?.headers.get("Idempotency-Key")).toMatch(
      /^[0-9a-f-]{36}$/,
    );
    const fresh = { ...(FAILING[2] as Feed), warning: null, fail_count: 0, last_error: null, last_ok_at: ago(0) };
    feeds = FAILING.map((f) => (f.id === 3 ? fresh : f));
    answer(Response.json(fresh));
    expect(await old().findByText("OK")).toBeInTheDocument();
    expect(old().getByText("just now")).toBeInTheDocument();
    expect(manage.getByRole("button", { name: "Failing 0" })).toBeInTheDocument();
  });

  it("says why a sync failed", async () => {
    setup({
      "POST /api/rss/feeds/3/refresh": () =>
        Response.json({ error: "FeedNotFound", detail: "no feed 3" }, { status: 404 }),
      "POST /api/rss/refresh": () =>
        Response.json({ error: "Busy", detail: "a poll is already running" }, { status: 409 }),
    });
    const manage = await openManage();
    await userEvent.click(rowOf(manage, "Old Blog").getByRole("button", { name: "Sync now" }));
    expect(
      await toast("Couldn't sync Old Blog: no feed 3. Try again later; Jesinia keeps retrying it on its own."),
    ).toBeDefined();
    await userEvent.click(manage.getByRole("button", { name: "Sync all" }));
    expect(await toast("Couldn't sync the feeds: a poll is already running. Try again in a moment.")).toBeDefined();
  });

  it("Sync all shows every row Syncing, then reloads", async () => {
    let answer: (r: Response) => void = () => {};
    const { calls } = setup({
      "POST /api/rss/refresh": () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    const manage = await openManage();
    const loads = sent(calls, "GET", "/api/rss/feeds").length;
    await userEvent.click(manage.getByRole("button", { name: "Sync all" }));
    await waitFor(() => expect(manage.getAllByText("Syncing")).toHaveLength(4));
    answer(Response.json({ polled: 4, new_items: 2, failed: 1 }));
    await waitFor(() => expect(manage.queryAllByText("Syncing")).toHaveLength(0));
    await waitFor(() => expect(sent(calls, "GET", "/api/rss/feeds").length).toBeGreaterThan(loads));
  });
});

describe("Retention", () => {
  /** The server keeps what a PUT sets, and the feeds' reload shows it. */
  function keepingRoutes(start: Feed[] = FAILING): Routes {
    let feeds = start;
    return {
      "GET /api/rss/feeds": () => Response.json(feeds),
      "PUT /api/rss/feeds/2/retention": async (req: Request) => {
        const { retention } = (await req.clone().json()) as Pick<Feed, "retention">;
        feeds = feeds.map((f) => (f.id === 2 ? { ...f, retention } : f));
        return Response.json(feeds[1]);
      },
    };
  }

  it("sets and clears a feed's retention", async () => {
    const { calls } = setup(keepingRoutes());
    const manage = await openManage();
    const keep = manage.getByRole("combobox", { name: "Keep posts for Fly.io blog" });
    expect(keep).toHaveDisplayValue("Default");
    await userEvent.selectOptions(keep, "Newest 200 posts");
    await waitFor(() => expect(sent(calls, "PUT", "/api/rss/feeds/2/retention")).toHaveLength(1));
    const put = sent(calls, "PUT", "/api/rss/feeds/2/retention")[0];
    expect(put?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await put?.json()).toEqual({ retention: { unit: "posts", value: 200 } });
    expect(keep).toHaveDisplayValue("Newest 200 posts");
    await userEvent.selectOptions(keep, "Default");
    await waitFor(() => expect(sent(calls, "PUT", "/api/rss/feeds/2/retention")).toHaveLength(2));
    expect(await sent(calls, "PUT", "/api/rss/feeds/2/retention")[1]?.json()).toEqual({ retention: null });
  });

  it("sets a custom retention, and shows a value outside the presets", async () => {
    const { calls } = setup(
      keepingRoutes(FAILING.map((f) => (f.id === 1 ? { ...f, retention: { unit: "days", value: 45 } } : f))),
    );
    const manage = await openManage();
    expect(manage.getByRole("combobox", { name: "Keep posts for Hacker News" })).toHaveDisplayValue("45 days");
    const keep = manage.getByRole("combobox", { name: "Keep posts for Fly.io blog" });
    await userEvent.selectOptions(keep, "Custom…");
    const value = manage.getByLabelText("Custom amount for Fly.io blog");
    expect(value).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(manage.queryByLabelText("Custom amount for Fly.io blog")).toBeNull();
    expect(keep).toHaveDisplayValue("Default");
    await userEvent.selectOptions(keep, "Custom…");
    await userEvent.click(manage.getByRole("button", { name: "Cancel" }));
    expect(manage.queryByLabelText("Custom amount for Fly.io blog")).toBeNull();
    await userEvent.selectOptions(keep, "Custom…");
    await userEvent.type(manage.getByLabelText("Custom amount for Fly.io blog"), "500");
    await userEvent.selectOptions(manage.getByRole("combobox", { name: "Unit for Fly.io blog" }), "posts");
    await userEvent.click(manage.getByRole("button", { name: "Set" }));
    await waitFor(() => expect(sent(calls, "PUT", "/api/rss/feeds/2/retention")).toHaveLength(1));
    expect(await sent(calls, "PUT", "/api/rss/feeds/2/retention")[0]?.json()).toEqual({
      retention: { unit: "posts", value: 500 },
    });
    expect(manage.queryByLabelText("Custom amount for Fly.io blog")).toBeNull();
    expect(keep).toHaveDisplayValue("Newest 500 posts");
  });

  it("shows the server's cause when a retention is refused, and puts the old value back", async () => {
    setup({
      "PUT /api/rss/feeds/2/retention": () =>
        Response.json({ error: "InvalidRetention", detail: "a posts cap must be 10 to 10000" }, { status: 400 }),
    });
    const manage = await openManage();
    const keep = manage.getByRole("combobox", { name: "Keep posts for Fly.io blog" });
    await userEvent.selectOptions(keep, "Custom…");
    await userEvent.type(manage.getByLabelText("Custom amount for Fly.io blog"), "5{Enter}");
    expect(
      await toast(
        "Couldn't change how long Fly.io blog keeps posts: a posts cap must be 10 to 10000. Choose 1–365 days or 10–10 000 posts.",
      ),
    ).toBeDefined();
    expect(keep).toHaveDisplayValue("Default");
  });

  it("changes the default retention", async () => {
    let answer: Response = Response.json({ retention: 90, source_folder: "Sources" });
    const { calls } = setup({
      "GET /api/rss/settings": { retention: 30, source_folder: "Sources" },
      "PUT /api/rss/settings": () => answer,
    });
    const manage = await openManage();
    const days = manage.getByRole("combobox", { name: "Keep posts for, all feeds" });
    await waitFor(() => expect(days).toHaveDisplayValue("30 days"));
    await userEvent.selectOptions(days, "90 days");
    await waitFor(() => expect(sent(calls, "PUT", "/api/rss/settings")).toHaveLength(1));
    const put = sent(calls, "PUT", "/api/rss/settings")[0];
    expect(put?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await put?.json()).toEqual({ retention: 90, source_folder: "Sources" });
    expect(await toast("Feeds without their own setting keep posts for 90 days.")).toBeDefined();

    answer = Response.json({ error: "InvalidRetention", detail: "retention must be 1 to 365 days" }, { status: 400 });
    await userEvent.selectOptions(days, "7 days");
    expect(
      await toast("Couldn't change how long posts are kept: retention must be 1 to 365 days. Choose 1–365 days."),
    ).toBeDefined();
    expect(days).toHaveDisplayValue("90 days");
  });

  it("says why the default retention didn't load", async () => {
    setup({ "GET /api/rss/settings": () => Response.json({ detail: "settings unreadable" }, { status: 500 }) });
    const manage = await openManage();
    expect(
      await toast("Couldn't load the default retention: settings unreadable. Open Manage feeds again to retry."),
    ).toBeDefined();
    expect(manage.getByRole("combobox", { name: "Keep posts for, all feeds" })).toBeDisabled();
  });
});

describe("OPML", () => {
  const opmlFile = (text: string, name = "feeds.opml") => new File([text], name, { type: "text/x-opml" });

  it("imports and exports OPML", async () => {
    const { calls } = setup({
      "POST /api/rss/opml": { added: 2, existing: 1, failed: [{ url: "https://bad.example/feed", cause: "HTTP 404" }] },
    });
    const manage = await openManage();
    const exportLink = manage.getByRole("link", { name: "Export OPML" });
    expect(exportLink).toHaveAttribute("href", "/api/rss/opml");
    expect(exportLink).toHaveAttribute("download", "jesinia-feeds.opml");
    const loads = sent(calls, "GET", "/api/rss/feeds").length;
    const opml = '<opml version="2.0"><body><outline xmlUrl="https://a.example/feed"/></body></opml>';
    await userEvent.upload(manage.getByLabelText("Import OPML"), opmlFile(opml));
    await waitFor(() => expect(sent(calls, "POST", "/api/rss/opml")).toHaveLength(1));
    const post = sent(calls, "POST", "/api/rss/opml")[0];
    expect(post?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await post?.json()).toEqual({ opml });
    expect(
      await toast(
        "Imported feeds.opml: added 2 feeds, skipped 1 already followed, 1 failed (https://bad.example/feed: HTTP 404).",
      ),
    ).toBeDefined();
    await waitFor(() => expect(sent(calls, "GET", "/api/rss/feeds").length).toBeGreaterThan(loads));
  });

  it("summarises a clean import", async () => {
    setup({ "POST /api/rss/opml": { added: 1, existing: 0, failed: [] } });
    const manage = await openManage();
    await userEvent.upload(manage.getByLabelText("Import OPML"), opmlFile("<opml/>"));
    expect(await toast("Imported feeds.opml: added 1 feed.")).toBeDefined();
  });

  it("refuses an oversize or empty file before sending it, and says why the server refused one", async () => {
    let code = 422;
    const { calls } = setup({
      "POST /api/rss/opml": () =>
        Response.json({ error: "InvalidOpml", detail: "not an OPML document" }, { status: code }),
    });
    const manage = await openManage();
    const big = opmlFile("<opml/>", "big.opml");
    Object.defineProperty(big, "size", { value: 12_000_000 });
    await userEvent.upload(manage.getByLabelText("Import OPML"), big);
    expect(
      await toast(
        "big.opml is 12 MB; Jesinia imports OPML files up to 5 MB. Export again with fewer feeds, or split the file.",
      ),
    ).toBeDefined();
    await userEvent.upload(manage.getByLabelText("Import OPML"), opmlFile(""));
    expect(await toast("feeds.opml is empty. Choose the OPML file your reader exported.")).toBeDefined();
    expect(sent(calls, "POST", "/api/rss/opml")).toHaveLength(0);
    await dismissAll(); // error toasts stay until dismissed, three at a time

    await userEvent.upload(manage.getByLabelText("Import OPML"), opmlFile("<opml/>"));
    expect(
      await toast(
        "Couldn't import feeds.opml: it is larger than the 5 MB Jesinia imports. Export again with fewer feeds, or split the file.",
      ),
    ).toBeDefined();
    code = 400;
    await userEvent.upload(manage.getByLabelText("Import OPML"), opmlFile("<html/>", "page.html"));
    expect(
      await toast(
        "Couldn't import page.html: not an OPML document. Export the list from your reader as OPML, then choose that file.",
      ),
    ).toBeDefined();
    const gone = opmlFile("<opml/>", "moved.opml");
    gone.text = () => Promise.reject(new Error("the file was moved"));
    await userEvent.upload(manage.getByLabelText("Import OPML"), gone);
    expect(await toast("Couldn't read moved.opml: the file was moved. Choose the file again.")).toBeDefined();
    expect(manage.getByLabelText("Import OPML")).toBeEnabled();
  });
});

describe("Remove", () => {
  it("remove waits for the toast and undo keeps the feed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let gone: number[] = [];
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(FAILING.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/4": () => {
        gone = [4];
        return new Response(null, { status: 204 });
      },
    });
    const manage = await openManage();
    // Remove, and the toast runs out: only then is it sent.
    await userEvent.click(rowOf(manage, "Loose feed").getByRole("button", { name: "Remove" }));
    expect(manage.queryByRole("row", { name: "Loose feed" })).toBeNull(); // hidden at once
    expect(manage.getByRole("button", { name: "All 3" })).toBeInTheDocument();
    expect(await toast("Removed Loose feed")).toBeDefined();
    const loads = sent(calls, "GET", "/api/rss/feeds").length;
    act(() => vi.advanceTimersByTime(7000));
    expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(0);
    act(() => vi.advanceTimersByTime(1500));
    await waitFor(() => expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(1));
    expect(sent(calls, "DELETE", "/api/rss/feeds/4")[0]?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    await waitFor(() => expect(sent(calls, "GET", "/api/rss/feeds").length).toBeGreaterThan(loads));
    expect(manage.queryByRole("row", { name: "Loose feed" })).toBeNull();

    // Remove, then Undo: nothing is sent and the row is back.
    await userEvent.click(rowOf(manage, "Fly.io blog").getByRole("button", { name: "Remove" }));
    expect(manage.queryByRole("row", { name: "Fly.io blog" })).toBeNull();
    const removed = (await toast("Removed Fly.io blog")) as HTMLElement;
    await userEvent.click(within(removed.closest("[data-toast]") as HTMLElement).getByRole("button", { name: "Undo" }));
    expect(await manage.findByRole("row", { name: "Fly.io blog" })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(9000));
    expect(calls.filter((c) => c.method === "DELETE" && c.url.endsWith("/api/rss/feeds/2"))).toHaveLength(0);
  });

  it("after an Undo, the next remove still times out and is sent", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let gone: number[] = [];
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(FAILING.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/4": () => {
        gone = [4];
        return new Response(null, { status: 204 });
      },
    });
    const manage = await openManage();
    await userEvent.click(rowOf(manage, "Loose feed").getByRole("button", { name: "Remove" }));
    const removed = (await toast("Removed Loose feed")) as HTMLElement;
    await userEvent.click(within(removed.closest("[data-toast]") as HTMLElement).getByRole("button", { name: "Undo" }));
    expect(await manage.findByRole("row", { name: "Loose feed" })).toBeInTheDocument();
    expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(0);

    await userEvent.click(rowOf(manage, "Loose feed").getByRole("button", { name: "Remove" }));
    expect(await toast("Removed Loose feed")).toBeDefined();
    act(() => vi.advanceTimersByTime(8500));
    await waitFor(() => expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(1));
    await waitFor(() => expect(manage.queryByRole("row", { name: "Loose feed" })).toBeNull());
  });

  it("removing the feed in view, then going back, lands on All unread", async () => {
    let gone: number[] = [];
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(FAILING.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/4": () => {
        gone = [4];
        return new Response(null, { status: 204 });
      },
    });
    const pane = within(await screen.findByRole("navigation", { name: "Feeds" }));
    await userEvent.click(await pane.findByRole("button", { name: /^Loose feed/ }));
    expect(await screen.findByRole("heading", { name: "Loose feed", level: 2 })).toBeInTheDocument();
    const manage = await openManage();
    await userEvent.click(rowOf(manage, "Loose feed").getByRole("button", { name: "Remove" }));
    expect(await toast("Removed Loose feed")).toBeDefined();
    expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(0);
    // Leaving sends the pending remove first, and its toast goes.
    await userEvent.click(screen.getByRole("button", { name: "‹ Back to reading" }));
    await waitFor(() => expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(1));
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
    expect(screen.queryAllByText("Removed Loose feed").filter((el) => el.closest("[data-toast]"))).toHaveLength(0);
  });

  it("removing the last feed of the folder in view goes back to All unread; another feed leaves the view", async () => {
    let gone: number[] = [];
    const drop = (id: number) => () => {
      gone = [...gone, id];
      return new Response(null, { status: 204 });
    };
    setup({
      "GET /api/rss/feeds": () => Response.json(FAILING.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/1": drop(1),
      "DELETE /api/rss/feeds/2": drop(2),
    });
    const pane = within(await screen.findByRole("navigation", { name: "Feeds" }));
    await userEvent.click(await pane.findByRole("button", { name: /^Engineering/ }));
    expect(await screen.findByRole("heading", { name: "Engineering", level: 2 })).toBeInTheDocument();
    let manage = await openManage();
    await userEvent.click(rowOf(manage, "Fly.io blog").getByRole("button", { name: "Remove" }));
    await userEvent.click(screen.getByRole("button", { name: "‹ Back to reading" }));
    await waitFor(() => expect(gone).toEqual([2]));
    expect(screen.getByRole("heading", { name: "Engineering", level: 2 })).toBeInTheDocument();

    await userEvent.click(pane.getByRole("button", { name: /^News/ }));
    expect(await screen.findByRole("heading", { name: "News", level: 2 })).toBeInTheDocument();
    manage = await openManage();
    await userEvent.click(rowOf(manage, "Hacker News").getByRole("button", { name: "Remove" }));
    const removed = (await toast("Removed Hacker News")) as HTMLElement;
    await userEvent.click(
      within(removed.closest("[data-toast]") as HTMLElement).getByRole("button", { name: "Dismiss" }),
    );
    await waitFor(() => expect(gone).toEqual([2, 1]));
    await userEvent.click(screen.getByRole("button", { name: "‹ Back to reading" }));
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
  });

  it("says why a remove failed, and the row comes back", async () => {
    setup({
      "DELETE /api/rss/feeds/2": () => Response.json({ error: "FeedNotFound", detail: "no feed 2" }, { status: 404 }),
    });
    const manage = await openManage();
    await userEvent.click(rowOf(manage, "Fly.io blog").getByRole("button", { name: "Remove" }));
    const removed = (await toast("Removed Fly.io blog")) as HTMLElement;
    await userEvent.click(
      within(removed.closest("[data-toast]") as HTMLElement).getByRole("button", { name: "Dismiss" }),
    );
    expect(await toast("Couldn't remove Fly.io blog: no feed 2. Try Remove again.")).toBeDefined();
    expect(await manage.findByRole("row", { name: "Fly.io blog" })).toBeInTheDocument();
  });
});
