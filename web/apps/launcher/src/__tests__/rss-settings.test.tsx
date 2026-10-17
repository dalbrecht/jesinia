// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → RSS (design pass): OPML import and export, retention and the kept-posts folder,
// in the book layout (an outline on the left, the view pane on the right).
import type { components } from "@jesinia/client";
import { baseRoutes, fakeClient } from "@jesinia/shell";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";

type Feed = components["schemas"]["FeedOut"];
const FEEDS: Feed[] = [
  { id: 1, title: "A", url: "https://a.example/feed", folder: "News", folder_id: 1, unread: 0, warning: null },
  { id: 2, title: "B", url: "https://b.example/feed", folder: "News", folder_id: 1, unread: 0, warning: null },
  { id: 3, title: "C", url: "https://c.example/feed", folder: "Eng", folder_id: 2, unread: 0, warning: null },
  { id: 4, title: "D", url: "https://d.example/feed", folder: null, folder_id: null, unread: 0, warning: null },
];
const SETTINGS = { retention: 30, source_folder: "Sources" };

/** Settings opened at `hash` (R27: one section per view; RSS by default here). */
function renderRss(over = {}, hash = "#rss") {
  window.history.replaceState(null, "", `/settings/${hash}`);
  const { client, calls } = fakeClient(
    baseRoutes({ "GET /api/rss/feeds": FEEDS, "GET /api/rss/settings": SETTINGS, ...over }),
  );
  render(<App path="/settings/" client={client} />);
  return calls;
}

const section = async () => within(await screen.findByRole("region", { name: "RSS" }));
const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const toast = async (text: string) =>
  (await screen.findAllByText(text)).find((el) => el.closest("[data-toast]") !== null);
const opmlFile = (text: string) => new File([text], "feeds.opml", { type: "text/x-opml" });

afterEach(() => window.history.replaceState(null, "", "/settings/"));

describe("Settings layout (R27: one section per view)", () => {
  it("has an outline of sections; the view pane shows only the chosen one", async () => {
    renderRss({}, "");
    const outline = screen.getByRole("navigation", { name: "Settings sections" });
    const links = within(outline)
      .getAllByRole("link")
      .map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([
      ["Knowledge base", "#kb"],
      ["Git sync", "#sync"],
      ["Agents", "#agents"],
      ["Tags", "#tags"],
      ["RSS", "#rss"],
      ["Conflicts", "#conflicts"],
      ["Security", "#security"],
    ]);
    const pane = screen.getByRole("region", { name: "Settings" });
    expect(within(pane).getByRole("heading", { name: "Settings", level: 1 })).toBeInTheDocument();
    // With no section in the address, the first one shows, alone.
    const kb = within(outline).getByRole("link", { name: "Knowledge base" });
    expect(kb).toHaveAttribute("aria-current", "location");
    expect(within(pane).getByRole("region", { name: "Knowledge base" })).toBeInTheDocument();
    expect(within(pane).queryByRole("region", { name: "RSS" })).toBeNull();
    expect(within(pane).queryByRole("region", { name: "Git sync" })).toBeNull();

    const rss = within(outline).getByRole("link", { name: "RSS" });
    await userEvent.click(rss);
    await waitFor(() => expect(rss).toHaveAttribute("aria-current", "location"));
    expect(kb).not.toHaveAttribute("aria-current");
    expect(within(pane).getByRole("region", { name: "RSS" })).toBeInTheDocument();
    expect(within(pane).queryByRole("region", { name: "Knowledge base" })).toBeNull();
    // The section's title takes focus, so a keyboard or screen-reader user lands on it.
    expect(within(pane).getByRole("heading", { name: "RSS", level: 2 })).toHaveFocus();
  });

  it("opens the section named in the address, and falls back to the first for an unknown one", () => {
    renderRss({}, "#tags");
    const outline = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(outline).getByRole("link", { name: "Tags" })).toHaveAttribute("aria-current", "location");
    expect(screen.getByRole("region", { name: "Tags" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Knowledge base" })).toBeNull();
    cleanup();
    renderRss({}, "#nope");
    expect(screen.getByRole("region", { name: "Knowledge base" })).toBeInTheDocument();
  });
});

describe("Settings → RSS", () => {
  it("shows how many feeds and folders, the retention and the kept-posts folder", async () => {
    renderRss();
    const rss = await section();
    expect(await rss.findByText("4 feeds in 2 folders")).toBeInTheDocument();
    expect(await rss.findByRole("combobox", { name: "Keep unread posts for" })).toHaveValue("30");
    expect(rss.getByText("Kept posts stay forever.")).toBeInTheDocument();
    expect(rss.getByLabelText("Folder for kept posts")).toHaveValue("Sources");
    const download = rss.getByRole("link", { name: "Download OPML" });
    expect(download).toHaveAttribute("href", "/api/rss/opml");
    expect(download).toHaveAttribute("download");
  });

  it("counts one feed and one folder in the singular", async () => {
    renderRss({ "GET /api/rss/feeds": [FEEDS[0]] });
    expect(await (await section()).findByText("1 feed in 1 folder")).toBeInTheDocument();
  });

  it("saves retention as soon as it changes, with a fresh key", async () => {
    const calls = renderRss({
      "PUT /api/rss/settings": async (req: Request) => Response.json(await req.clone().json()),
    });
    const rss = await section();
    const retention = await rss.findByRole("combobox", { name: "Keep unread posts for" });
    await waitFor(() => expect(retention).toHaveValue("30"));
    await userEvent.selectOptions(retention, "90");
    await waitFor(() => expect(sent(calls, "PUT", "/api/rss/settings")).toHaveLength(1));
    const put = sent(calls, "PUT", "/api/rss/settings")[0];
    expect(put?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await put?.json()).toEqual({ retention: 90, source_folder: "Sources" });
    expect(await toast("Unread posts are kept for 90 days.")).toBeDefined();
    expect(retention).toHaveValue("90");
  });

  it("offers a retention set outside the usual choices", async () => {
    renderRss({ "GET /api/rss/settings": { retention: 45, source_folder: "Sources" } });
    const retention = await (await section()).findByRole("combobox", { name: "Keep unread posts for" });
    await waitFor(() => expect(retention).toHaveValue("45"));
    expect(within(retention).getByRole("option", { name: "45 days" })).toBeInTheDocument();
  });

  it("saves the kept-posts folder, and shows why a folder is refused", async () => {
    let answer: Response = Response.json({
      error: "InvalidSettings",
      detail: "Journal: Jesinia never writes pages in reserved, hidden or _assets folders",
    }, { status: 400 }); // prettier-ignore
    const calls = renderRss({ "PUT /api/rss/settings": () => answer });
    const rss = await section();
    const folder = rss.getByLabelText("Folder for kept posts");
    await waitFor(() => expect(folder).toHaveValue("Sources"));
    await userEvent.clear(folder);
    await userEvent.type(folder, "Journal");
    await userEvent.click(rss.getByRole("button", { name: "Save folder" }));
    expect(
      await rss.findByText("Journal: Jesinia never writes pages in reserved, hidden or _assets folders"),
    ).toBeInTheDocument();
    expect(folder).toHaveAttribute("aria-invalid", "true");
    expect(await sent(calls, "PUT", "/api/rss/settings")[0]?.json()).toEqual({
      retention: 30,
      source_folder: "Journal",
    });

    answer = Response.json({ retention: 30, source_folder: "Reading/Sources" });
    await userEvent.clear(folder);
    await userEvent.type(folder, " Reading/Sources {Enter}");
    expect(await toast("Kept posts now go to Reading/Sources/.")).toBeDefined();
    expect(folder).toHaveValue("Reading/Sources");
    expect(folder).not.toHaveAttribute("aria-invalid");
  });

  it("says why the RSS settings could not be loaded", async () => {
    renderRss({
      "GET /api/rss/settings": () => Response.json({ detail: "rss module not started" }, { status: 503 }),
      "GET /api/rss/feeds": () => Response.json({ detail: "rss module not started" }, { status: 503 }),
    });
    const rss = await section();
    expect(await rss.findByText("Couldn't load the RSS settings: rss module not started")).toBeInTheDocument();
    expect(rss.queryByRole("combobox", { name: "Keep unread posts for" })).toBeNull();
  });

  it("reports a failed retention save and keeps the saved value", async () => {
    renderRss({
      "PUT /api/rss/settings": () =>
        Response.json({ error: "InvalidSettings", detail: "retention must be 1 to 365 days" }, { status: 400 }),
    });
    const retention = await (await section()).findByRole("combobox", { name: "Keep unread posts for" });
    await waitFor(() => expect(retention).toHaveValue("30"));
    await userEvent.selectOptions(retention, "7");
    expect(await toast("Couldn't save the retention: retention must be 1 to 365 days")).toBeDefined();
    expect(retention).toHaveValue("30");
  });

  it("imports an OPML file and lists what was added, skipped and refused", async () => {
    let feeds = FEEDS;
    const calls = renderRss({
      "GET /api/rss/feeds": () => Response.json(feeds),
      "POST /api/rss/opml": () => {
        const loose = FEEDS[3] as Feed;
        feeds = [...FEEDS, { ...loose, id: 5 }, { ...loose, id: 6 }];
        return Response.json({
          added: 2,
          existing: 1,
          failed: [{ url: "ftp://old.example/feed", cause: "only http(s) feed URLs are supported" }],
        });
      },
    });
    const rss = await section();
    await rss.findByText("4 feeds in 2 folders");
    const opml = '<opml version="2.0"><body><outline text="Reading"/></body></opml>';
    await userEvent.upload(rss.getByLabelText("Import OPML"), opmlFile(opml));
    await waitFor(() => expect(sent(calls, "POST", "/api/rss/opml")).toHaveLength(1));
    const post = sent(calls, "POST", "/api/rss/opml")[0];
    expect(post?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await post?.json()).toEqual({ opml });
    const result = await rss.findByRole("status");
    expect(result).toHaveTextContent("Added 2 feeds. 1 was already followed. 1 couldn't be added:");
    expect(within(result).getByText("ftp://old.example/feed")).toBeInTheDocument();
    expect(within(result).getByText("only http(s) feed URLs are supported")).toBeInTheDocument();
    expect(await rss.findByText("6 feeds in 2 folders")).toBeInTheDocument();
  });

  it("says when an import added one feed and nothing else happened", async () => {
    renderRss({ "POST /api/rss/opml": { added: 1, existing: 0, failed: [] } });
    const rss = await section();
    await userEvent.upload(rss.getByLabelText("Import OPML"), opmlFile("<opml/>"));
    expect(await rss.findByRole("status")).toHaveTextContent(/^Added 1 feed\.$/);
  });

  it("shows why an import was refused", async () => {
    renderRss({
      "POST /api/rss/opml": () =>
        Response.json({ error: "InvalidOpml", detail: "not an OPML document: no body" }, { status: 400 }),
    });
    const rss = await section();
    await userEvent.upload(rss.getByLabelText("Import OPML"), opmlFile("<html/>"));
    expect(await rss.findByRole("alert")).toHaveTextContent(
      "Couldn't import feeds.opml: not an OPML document: no body",
    );
  });

  it("refreshes every feed now and says what came in", async () => {
    const calls = renderRss({ "POST /api/rss/refresh": { polled: 4, new_items: 7, failed: 1 } });
    const rss = await section();
    await userEvent.click(rss.getByRole("button", { name: "Refresh all feeds now" }));
    expect(await toast("Polled 4 feeds: 7 new posts, 1 feed failed.")).toBeDefined();
    expect(sent(calls, "POST", "/api/rss/refresh")[0]?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("refresh says when nothing came in, and when it failed", async () => {
    let answer = Response.json({ polled: 1, new_items: 0, failed: 0 });
    renderRss({ "POST /api/rss/refresh": () => answer });
    const rss = await section();
    await userEvent.click(rss.getByRole("button", { name: "Refresh all feeds now" }));
    expect(await toast("Polled 1 feed: no new posts.")).toBeDefined();
    answer = Response.json({ detail: "rss module not started" }, { status: 503 });
    await userEvent.click(rss.getByRole("button", { name: "Refresh all feeds now" }));
    expect(await toast("Couldn't refresh: rss module not started")).toBeDefined();
  });
});
