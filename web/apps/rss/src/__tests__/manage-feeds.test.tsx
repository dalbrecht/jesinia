// SPDX-License-Identifier: AGPL-3.0-or-later
// Feed management (spec §4.1, §7): + Add feed (a feed or a site), folders, unsubscribing, and
// a failing feed's cause with Retry.
import { fakeClient, type Routes } from "@jesinia/shell";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import type { Feed } from "../api";
import { FEEDS, routes } from "./fixtures";

const ADDED: Feed = {
  id: 5,
  title: "Example Blog",
  url: "https://example.com/feed.xml",
  folder: "Reading",
  folder_id: 12,
  unread: 3,
  warning: null,
};

function setup(over: Routes = {}) {
  const env = fakeClient(routes(over));
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const dialog = () => screen.getByRole("dialog", { name: "Feeds" });
const toast = async (text: string | RegExp) =>
  (await screen.findAllByText(text)).find((el) => el.closest("[data-toast]") !== null);

/**
 * Enters text the way a paste does: one input event. Typing per keystroke re-renders the whole
 * app each time, and on a loaded CI runner long strings took tests past the 5 s timeout.
 */
async function enter(field: HTMLElement, text: string): Promise<void> {
  await userEvent.click(field);
  await userEvent.paste(text);
}

/** The dialog once the feeds have loaded: its rows exist only then (never query before). */
async function openManage() {
  await userEvent.click(await screen.findByRole("button", { name: "+ Add feed" }));
  const manage = within(dialog());
  await manage.findAllByRole("listitem");
  return manage;
}

describe("Add feed", () => {
  it("adds a site or feed URL into a folder, with a fresh key, then shows the new feed", async () => {
    let feeds = FEEDS;
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "POST /api/rss/feeds": () => {
        feeds = [...FEEDS, ADDED];
        return Response.json(ADDED, { status: 201 });
      },
    });
    const manage = await openManage();
    expect(manage.getByLabelText("Feed or site URL")).toHaveFocus();
    // Pasted, not typed: each typed character re-renders the whole app, and on a loaded CI
    // runner 28 of them took this test past the 5 s timeout. A paste is one input event with
    // the same text (the spaces still test the trim).
    await userEvent.click(manage.getByLabelText("Feed or site URL"));
    await userEvent.paste(" https://example.com ");
    await userEvent.click(manage.getByLabelText("Folder (optional)"));
    await userEvent.paste("Reading");
    await userEvent.click(manage.getByRole("button", { name: "Add feed" }));
    await waitFor(() => expect(sent(calls, "POST", "/api/rss/feeds")).toHaveLength(1));
    const req = sent(calls, "POST", "/api/rss/feeds")[0];
    expect(req?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await req?.json()).toEqual({ url: "https://example.com", folder: "Reading" });
    expect(await toast("Added Example Blog.")).toBeDefined();
    expect(screen.queryByRole("dialog", { name: "Feeds" })).toBeNull();
    // The list now shows the new feed.
    expect(await screen.findByRole("heading", { name: "Example Blog", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Example Blog/ })).toHaveAttribute("aria-current", "true");
  });

  it("adds without a folder, and says why a feed could not be added", async () => {
    let answer = Response.json(
      { error: "FeedUnavailable", detail: "no feed found at https://plain.example/: the page links to none" },
      { status: 502 },
    );
    const { calls } = setup({ "POST /api/rss/feeds": () => answer });
    const manage = await openManage();
    const add = manage.getByRole("button", { name: "Add feed" });
    expect(add).toBeDisabled(); // nothing typed yet
    await enter(manage.getByLabelText("Feed or site URL"), "https://plain.example/");
    await userEvent.keyboard("{Enter}");
    expect(
      await manage.findByText(
        "Couldn't add the feed: no feed found at https://plain.example/: the page links to none.",
      ),
    ).toBeInTheDocument();
    expect(await sent(calls, "POST", "/api/rss/feeds")[0]?.json()).toEqual({
      url: "https://plain.example/",
      folder: null,
    });
    // A second try is a new action with its own key.
    answer = Response.json(ADDED, { status: 201 });
    await userEvent.click(add);
    await waitFor(() => expect(sent(calls, "POST", "/api/rss/feeds")).toHaveLength(2));
    const [first, second] = sent(calls, "POST", "/api/rss/feeds");
    expect(first?.headers.get("Idempotency-Key")).not.toEqual(second?.headers.get("Idempotency-Key"));
    expect(await toast("Added Example Blog.")).toBeDefined();
  });

  it("offers the existing folders, closes on Esc and Close, and keys wait while it is open", async () => {
    setup();
    let manage = await openManage();
    const options = [...document.querySelectorAll("#rss-folder-names option")].map((o) => o.getAttribute("value"));
    expect(options).toEqual(["Engineering", "News"]);
    fireEvent.keyDown(document.body, { key: "j" });
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull(); // j did not open a post
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Feeds" })).toBeNull();
    expect(screen.getByRole("button", { name: "+ Add feed" })).toHaveFocus();
    manage = await openManage();
    await userEvent.click(manage.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Feeds" })).toBeNull();
  });

  it("keeps Tab inside the dialog, and an empty URL sends nothing", async () => {
    const { calls } = setup();
    const manage = await openManage();
    const field = manage.getByLabelText("Feed or site URL");
    const close = manage.getByRole("button", { name: "Close" });
    await userEvent.type(field, "   {Enter}");
    expect(sent(calls, "POST", "/api/rss/feeds")).toHaveLength(0);
    await userEvent.tab();
    expect(manage.getByLabelText("Folder (optional)")).toHaveFocus(); // within: Tab moves on
    await userEvent.tab({ shift: true });
    expect(field).toHaveFocus();
    close.focus();
    await userEvent.tab();
    expect(field).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(close).toHaveFocus();
    await userEvent.click(screen.getByRole("dialog", { name: "Feeds" }).parentElement as HTMLElement);
    expect(screen.queryByRole("dialog", { name: "Feeds" })).toBeNull(); // a click outside closes it
  });
});

describe("Managing feeds and folders", () => {
  it("moves a feed to another folder or none", async () => {
    const { calls } = setup({
      "PATCH /api/rss/feeds/2": async (req: Request) =>
        Response.json({ ...FEEDS[1], ...((await req.clone().json()) as object) }),
    });
    const manage = await openManage();
    expect(manage.getByRole("list", { name: "Feeds in Engineering" })).toBeInTheDocument();
    expect(manage.getByRole("list", { name: "Feeds in no folder" })).toBeInTheDocument();
    const folder = manage.getByRole("combobox", { name: "Folder for Fly.io blog" });
    expect(folder).toHaveValue("Engineering");
    await userEvent.selectOptions(folder, "News");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(1));
    const move = sent(calls, "PATCH", "/api/rss/feeds/2")[0];
    expect(move?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await move?.json()).toEqual({ folder: "News" });
    await userEvent.selectOptions(folder, "");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(2));
    expect(await sent(calls, "PATCH", "/api/rss/feeds/2")[1]?.json()).toEqual({ folder: null });
    await waitFor(() => expect(sent(calls, "GET", "/api/rss/feeds").length).toBeGreaterThan(2));
  });

  it("renames a folder and reports a clash", async () => {
    let answer = Response.json({ id: 11, name: "Eng" });
    const { calls } = setup({ "PATCH /api/rss/folders/11": () => answer });
    const manage = await openManage();
    await userEvent.click(manage.getByRole("button", { name: "Rename Engineering" }));
    const name = manage.getByLabelText("New name for Engineering");
    expect(name).toHaveFocus();
    expect(name).toHaveValue("Engineering");
    await userEvent.clear(name);
    await userEvent.type(name, "  {Enter}");
    expect(sent(calls, "PATCH", "/api/rss/folders/11")).toHaveLength(0); // no name, nothing sent
    await userEvent.clear(name);
    await userEvent.type(name, "Eng{Enter}");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/folders/11")).toHaveLength(1));
    const rename = sent(calls, "PATCH", "/api/rss/folders/11")[0];
    expect(rename?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await rename?.json()).toEqual({ name: "Eng" });
    expect(await toast("Renamed Engineering to Eng.")).toBeDefined();

    answer = Response.json({ error: "FolderExists", detail: "a folder named News already exists" }, { status: 409 });
    await userEvent.click(manage.getByRole("button", { name: "Rename Engineering" }));
    await userEvent.clear(manage.getByLabelText("New name for Engineering"));
    await userEvent.type(manage.getByLabelText("New name for Engineering"), "News");
    await userEvent.click(manage.getByRole("button", { name: "Save name" }));
    expect(
      await manage.findByText("Couldn't rename the folder: a folder named News already exists."),
    ).toBeInTheDocument();
    await userEvent.click(manage.getByRole("button", { name: "Cancel" }));
    expect(manage.queryByLabelText("New name for Engineering")).toBeNull();
  });

  it("unsubscribes only after confirming, and says kept posts stay", async () => {
    let gone: number[] = [];
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(FEEDS.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/4": () => {
        gone = [4];
        return new Response(null, { status: 204 });
      },
    });
    const manage = await openManage();
    const row = () => within(manage.getByRole("listitem", { name: "Loose feed" }));
    await userEvent.click(row().getByRole("button", { name: "Unsubscribe" }));
    expect(row().getByText("Its unkept posts go; kept posts stay in the KB.")).toBeInTheDocument();
    await userEvent.click(row().getByRole("button", { name: "Cancel" }));
    expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(0);
    await userEvent.click(row().getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row().getByRole("button", { name: "Unsubscribe from Loose feed" }));
    await waitFor(() => expect(sent(calls, "DELETE", "/api/rss/feeds/4")).toHaveLength(1));
    expect(sent(calls, "DELETE", "/api/rss/feeds/4")[0]?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await toast("Unsubscribed from Loose feed.")).toBeDefined();
    await waitFor(() => expect(manage.queryByRole("listitem", { name: "Loose feed" })).toBeNull());
  });

  it("reports a failed move or unsubscribe with the cause", async () => {
    setup({
      "PATCH /api/rss/feeds/2": () => Response.json({ error: "FeedNotFound", detail: "no feed 2" }, { status: 404 }),
      "DELETE /api/rss/feeds/2": () => Response.json({ error: "FeedNotFound", detail: "no feed 2" }, { status: 404 }),
    });
    const manage = await openManage();
    await userEvent.selectOptions(manage.getByRole("combobox", { name: "Folder for Fly.io blog" }), "News");
    expect(await toast("Couldn't move Fly.io blog: no feed 2.")).toBeDefined();
    const row = within(manage.getByRole("listitem", { name: "Fly.io blog" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe from Fly.io blog" }));
    expect(await toast("Couldn't unsubscribe from Fly.io blog: no feed 2.")).toBeDefined();
  });
});

describe("A failing feed", () => {
  const OLD_BLOG = FEEDS[2] as Feed;

  it("shows its cause with Retry in the feeds pane; Retry polls it now", async () => {
    let feeds = FEEDS;
    const { calls } = setup({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "POST /api/rss/feeds/3/refresh": () => {
        feeds = FEEDS.map((f) => (f.id === 3 ? { ...f, warning: null } : f));
        return Response.json({ ...OLD_BLOG, warning: null });
      },
    });
    const pane = within(await screen.findByRole("navigation", { name: "Feeds" }));
    expect(await pane.findByText("Failing for 3 days: 404 from old.example")).toBeInTheDocument();
    const problem = within(pane.getByRole("group", { name: "Old Blog is failing" }));
    await userEvent.click(problem.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(sent(calls, "POST", "/api/rss/feeds/3/refresh")).toHaveLength(1));
    const retry = sent(calls, "POST", "/api/rss/feeds/3/refresh")[0];
    expect(retry?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await toast("Old Blog is back.")).toBeDefined();
    await waitFor(() => expect(pane.queryByText("Failing for 3 days: 404 from old.example")).toBeNull());
  });

  it("says when it still fails, or when Retry itself failed, and also offers Retry in the dialog", async () => {
    let answer = Response.json({ ...OLD_BLOG, warning: "HTTP 503" });
    setup({ "POST /api/rss/feeds/3/refresh": () => answer });
    const manage = await openManage();
    const row = within(manage.getByRole("listitem", { name: "Old Blog" }));
    expect(row.getByText("Failing for 3 days: 404 from old.example")).toBeInTheDocument();
    await userEvent.click(row.getByRole("button", { name: "Retry" }));
    expect(await toast("Old Blog still fails: HTTP 503.")).toBeDefined();
    answer = Response.json({ error: "FeedNotFound", detail: "no feed 3" }, { status: 404 });
    await userEvent.click(row.getByRole("button", { name: "Retry" }));
    expect(
      await toast("Couldn't retry Old Blog: no feed 3. Try again later; Jesinia keeps retrying it on its own."),
    ).toBeDefined();
  });
});

describe("Focus stays in the dialog (I-2) and inline edits own their keys (M-1, M-2)", () => {
  it("unsubscribing moves focus to the next row, or the previous one at the end", async () => {
    let gone: number[] = [];
    const drop = (id: number) => () => {
      gone = [...gone, id];
      return new Response(null, { status: 204 });
    };
    setup({
      "GET /api/rss/feeds": () => Response.json(FEEDS.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/2": drop(2),
      "DELETE /api/rss/feeds/4": drop(4),
    });
    const manage = await openManage();
    const row = (name: string) => within(manage.getByRole("listitem", { name }));
    await userEvent.click(row("Fly.io blog").getByRole("button", { name: "Unsubscribe" }));
    expect(row("Fly.io blog").getByRole("button", { name: "Unsubscribe from Fly.io blog" })).toHaveFocus();
    await userEvent.click(row("Fly.io blog").getByRole("button", { name: "Unsubscribe from Fly.io blog" }));
    await waitFor(() => expect(row("Old Blog").getByRole("button", { name: "Unsubscribe" })).toHaveFocus());
    await waitFor(() => expect(manage.queryByRole("listitem", { name: "Fly.io blog" })).toBeNull());
    expect(row("Old Blog").getByRole("button", { name: "Unsubscribe" })).toHaveFocus();
    // The last row: focus goes back to the one before it.
    await userEvent.click(row("Loose feed").getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row("Loose feed").getByRole("button", { name: "Unsubscribe from Loose feed" }));
    await waitFor(() => expect(manage.queryByRole("listitem", { name: "Loose feed" })).toBeNull());
    expect(row("Hacker News").getByRole("button", { name: "Unsubscribe" })).toHaveFocus();
  });

  it("unsubscribing the only feed moves focus to the dialog's title; Cancel returns to Unsubscribe", async () => {
    let feeds = [FEEDS[3] as Feed];
    setup({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "DELETE /api/rss/feeds/4": () => {
        feeds = [];
        return new Response(null, { status: 204 });
      },
    });
    const manage = await openManage();
    const row = within(manage.getByRole("listitem", { name: "Loose feed" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row.getByRole("button", { name: "Cancel" }));
    expect(row.getByRole("button", { name: "Unsubscribe" })).toHaveFocus();
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe from Loose feed" }));
    await waitFor(() => expect(manage.getByRole("heading", { name: "Feeds", level: 2 })).toHaveFocus());
  });

  it("after a rename, a Cancel or Esc, focus returns to the folder's Rename; Esc keeps the dialog", async () => {
    setup({ "PATCH /api/rss/folders/11": { id: 11, name: "Engineering" } });
    const manage = await openManage();
    const rename = () => manage.getByRole("button", { name: "Rename Engineering" });
    await userEvent.click(rename());
    await userEvent.click(manage.getByRole("button", { name: "Cancel" }));
    expect(rename()).toHaveFocus();
    await userEvent.click(rename());
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Feeds" })).toBeInTheDocument();
    expect(manage.queryByLabelText("New name for Engineering")).toBeNull();
    expect(rename()).toHaveFocus();
    await userEvent.click(rename());
    await userEvent.click(manage.getByRole("button", { name: "Save name" }));
    await waitFor(() => expect(rename()).toHaveFocus());
  });

  it("a drag that ends on the backdrop keeps the dialog and what was typed", async () => {
    setup();
    const manage = await openManage();
    const field = manage.getByLabelText("Feed or site URL");
    await enter(field, "https://example.com");
    const scrim = dialog().parentElement as HTMLElement;
    fireEvent.mouseDown(field);
    fireEvent.click(scrim);
    expect(screen.getByRole("dialog", { name: "Feeds" })).toBeInTheDocument();
    expect(field).toHaveValue("https://example.com");
    fireEvent.mouseDown(scrim);
    fireEvent.click(scrim);
    expect(screen.queryByRole("dialog", { name: "Feeds" })).toBeNull();
  });
});

describe("R28", () => {
  it("files a feed into a new folder", async () => {
    const { calls } = setup({
      "PATCH /api/rss/feeds/2": async (req: Request) =>
        Response.json({ ...FEEDS[1], ...((await req.clone().json()) as object) }),
    });
    const manage = await openManage();
    const pick = manage.getByRole("combobox", { name: "Folder for Fly.io blog" });
    await userEvent.selectOptions(pick, "New folder…");
    const name = manage.getByLabelText("New folder for Fly.io blog");
    expect(name).toHaveFocus();
    await userEvent.keyboard("{Escape}"); // cancels the new folder only
    expect(screen.getByRole("dialog", { name: "Feeds" })).toBeInTheDocument();
    expect(manage.queryByLabelText("New folder for Fly.io blog")).toBeNull();
    expect(pick).toHaveFocus();
    expect(pick).toHaveValue("Engineering");
    await userEvent.selectOptions(pick, "New folder…");
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "  {Enter}");
    expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(0); // no name, nothing sent
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "Reading{Enter}");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/rss/feeds/2")).toHaveLength(1));
    expect(await sent(calls, "PATCH", "/api/rss/feeds/2")[0]?.json()).toEqual({ folder: "Reading" });
    await waitFor(() => expect(manage.queryByLabelText("New folder for Fly.io blog")).toBeNull());
    expect(manage.getByRole("combobox", { name: "Folder for Fly.io blog" })).toHaveFocus();
    await userEvent.selectOptions(pick, "New folder…");
    await userEvent.click(manage.getByRole("button", { name: "Cancel" }));
    expect(manage.queryByLabelText("New folder for Fly.io blog")).toBeNull();
  });

  it("says why a feed couldn't move to a new folder, and keeps the field", async () => {
    setup({
      "PATCH /api/rss/feeds/2": () =>
        Response.json(
          { error: "InvalidFolderName", detail: "a folder name must be 1 to 100 characters" },
          { status: 400 },
        ),
    });
    const manage = await openManage();
    await userEvent.selectOptions(manage.getByRole("combobox", { name: "Folder for Fly.io blog" }), "New folder…");
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "Reading{Enter}");
    expect(await toast("Couldn't move Fly.io blog: a folder name must be 1 to 100 characters.")).toBeDefined();
    expect(manage.getByLabelText("New folder for Fly.io blog")).toHaveValue("Reading");
  });

  it("unsubscribing the feed in view goes back to All unread; another feed leaves the view alone", async () => {
    let gone: number[] = [];
    const drop = (id: number) => () => {
      gone = [...gone, id];
      return new Response(null, { status: 204 });
    };
    setup({
      "GET /api/rss/feeds": () => Response.json(FEEDS.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/2": drop(2),
      "DELETE /api/rss/feeds/4": drop(4),
    });
    const pane = within(await screen.findByRole("navigation", { name: "Feeds" }));
    await userEvent.click(await pane.findByRole("button", { name: /Loose feed/ }));
    expect(await screen.findByRole("heading", { name: "Loose feed", level: 2 })).toBeInTheDocument();
    const manage = await openManage();
    const row = (name: string) => within(manage.getByRole("listitem", { name }));
    await userEvent.click(row("Fly.io blog").getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row("Fly.io blog").getByRole("button", { name: "Unsubscribe from Fly.io blog" }));
    await waitFor(() => expect(manage.queryByRole("listitem", { name: "Fly.io blog" })).toBeNull());
    expect(screen.getByRole("heading", { name: "Loose feed", level: 2 })).toBeInTheDocument();
    await userEvent.click(row("Loose feed").getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row("Loose feed").getByRole("button", { name: "Unsubscribe from Loose feed" }));
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
  });
});

describe("Focus after a move (carried minor)", () => {
  it("a feed moved to another folder keeps focus on its folder select after the list reloads", async () => {
    let feeds = FEEDS;
    setup({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "PATCH /api/rss/feeds/2": async (req: Request) => {
        const { folder } = (await req.clone().json()) as { folder: string };
        const moved = { ...(FEEDS[1] as Feed), folder, folder_id: folder === "News" ? 10 : 12 };
        feeds = FEEDS.map((f) => (f.id === 2 ? moved : f));
        return Response.json(moved);
      },
    });
    const manage = await openManage();
    await userEvent.selectOptions(manage.getByRole("combobox", { name: "Folder for Fly.io blog" }), "News");
    await waitFor(() =>
      expect(
        within(manage.getByRole("list", { name: "Feeds in News" })).getByRole("combobox", {
          name: "Folder for Fly.io blog",
        }),
      ).toHaveFocus(),
    );
    // And through New folder…, into a folder that didn't exist.
    await userEvent.selectOptions(manage.getByRole("combobox", { name: "Folder for Fly.io blog" }), "New folder…");
    await userEvent.type(manage.getByLabelText("New folder for Fly.io blog"), "Reading{Enter}");
    await waitFor(() =>
      expect(
        within(manage.getByRole("list", { name: "Feeds in Reading" })).getByRole("combobox", {
          name: "Folder for Fly.io blog",
        }),
      ).toHaveFocus(),
    );
  });
});

describe("M11", () => {
  it("unsubscribing the last feed of the folder in view goes back to All unread", async () => {
    let gone: number[] = [];
    setup({
      "GET /api/rss/feeds": () => Response.json(FEEDS.filter((f) => !gone.includes(f.id))),
      "DELETE /api/rss/feeds/1": () => {
        gone = [1];
        return new Response(null, { status: 204 });
      },
    });
    const pane = within(await screen.findByRole("navigation", { name: "Feeds" }));
    await userEvent.click(await pane.findByRole("button", { name: /^News/ }));
    expect(await screen.findByRole("heading", { name: "News", level: 2 })).toBeInTheDocument();
    const manage = await openManage();
    const row = within(manage.getByRole("listitem", { name: "Hacker News" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe from Hacker News" }));
    expect(await screen.findByRole("heading", { name: "All unread", level: 2 })).toBeInTheDocument();
  });

  it("unsubscribing one of several feeds in the folder in view leaves the view", async () => {
    setup({ "DELETE /api/rss/feeds/2": () => new Response(null, { status: 204 }) });
    const pane = within(await screen.findByRole("navigation", { name: "Feeds" }));
    await userEvent.click(await pane.findByRole("button", { name: /^Engineering/ }));
    expect(await screen.findByRole("heading", { name: "Engineering", level: 2 })).toBeInTheDocument();
    const manage = await openManage();
    const row = within(manage.getByRole("listitem", { name: "Fly.io blog" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe" }));
    await userEvent.click(row.getByRole("button", { name: "Unsubscribe from Fly.io blog" }));
    expect(await toast("Unsubscribed from Fly.io blog.")).toBeDefined();
    expect(screen.getByRole("heading", { name: "Engineering", level: 2 })).toBeInTheDocument();
  });
});
