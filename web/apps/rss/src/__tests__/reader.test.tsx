// SPDX-License-Identifier: AGPL-3.0-or-later
import { fakeClient, type Routes } from "@jesinia/shell";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { post, routes } from "./fixtures";

function setup(over: Routes = {}) {
  const env = fakeClient(routes(over));
  render(<App client={env.client} />);
  return env;
}
const gets = (calls: Request[], path: string) =>
  calls.filter((c) => c.method === "GET" && new URL(c.url).pathname === path);
async function option(name: RegExp | string) {
  return waitFor(() => within(screen.getByRole("listbox", { name: "Posts" })).getByRole("option", { name }));
}
const reader = () => screen.getByRole("region", { name: "Reader" });
const items = (...ps: ReturnType<typeof post>[]) => ({ "GET /api/rss/posts": { items: ps, next_before: null } });

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.stubGlobal("matchMedia", undefined);
  vi.restoreAllMocks();
});

describe("reader: automatic full article", () => {
  it("loads the full article without a click", async () => {
    const { calls } = setup({
      ...items(post(1, { is_summary: true, read: true })),
      "GET /api/rss/posts/1/article": { markdown: "## The whole thing\n\nEvery word." },
    });
    await userEvent.click(await option(/Post 1/));
    expect(await within(reader()).findByRole("heading", { name: "The whole thing" })).toBeInTheDocument();
    expect(gets(calls, "/api/rss/posts/1/article")).toHaveLength(1);
    expect(within(reader()).queryByRole("button", { name: "Load full article" })).toBeNull();
    expect(reader()).toHaveTextContent("Showing full article");
  });

  it("falls back quietly", async () => {
    const { calls } = setup({
      ...items(post(1, { is_summary: true, read: true, content_html: "<p>Feed words</p>" })),
      "GET /api/rss/posts/1/article": () =>
        Response.json({ error: "ArticleUnavailable", detail: "The site answered 403." }, { status: 502 }),
    });
    await userEvent.click(await option(/Post 1/));
    await waitFor(() => expect(gets(calls, "/api/rss/posts/1/article")).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(reader()).toHaveTextContent("Feed words");
    expect(reader()).not.toHaveTextContent(/couldn't load/i);
    expect(reader()).not.toHaveTextContent(/Showing/);
    expect(within(reader()).queryByRole("alert")).toBeNull();
  });

  it("toggles between full article and feed text", async () => {
    setup({
      ...items(post(1, { read: true, content_html: "<p>Feed words</p>" })),
      "GET /api/rss/posts/1/article": { markdown: "Extracted words" },
    });
    await userEvent.click(await option(/Post 1/));
    expect(await within(reader()).findByText("Extracted words")).toBeInTheDocument();
    expect(reader()).toHaveTextContent("Showing full article · Show feed text");
    await userEvent.click(within(reader()).getByRole("button", { name: "Show feed text" }));
    expect(within(reader()).getByText("Feed words")).toBeInTheDocument();
    expect(within(reader()).queryByText("Extracted words")).toBeNull();
    expect(reader()).toHaveTextContent("Showing feed text · Show full article");
    await userEvent.click(within(reader()).getByRole("button", { name: "Show full article" }));
    expect(within(reader()).getByText("Extracted words")).toBeInTheDocument();
  });

  it("ignores an article that arrives after moving on", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    let release: (r: Response) => void = () => {};
    const { calls } = setup({
      ...items(post(1, { read: true }), post(2, { read: true })),
      "GET /api/rss/posts/1/article": () => new Promise<Response>((res) => (release = res)),
      "GET /api/rss/posts/2/article": () => new Promise<Response>(() => {}),
    });
    await userEvent.click(await option(/Post 1/));
    await waitFor(() => expect(gets(calls, "/api/rss/posts/1/article")).toHaveLength(1));
    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() => expect(gets(calls, "/api/rss/posts/2/article")).toHaveLength(1));
    release(Response.json({ markdown: "POST ONE ARTICLE" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(reader()).not.toHaveTextContent("POST ONE ARTICLE");
    expect(reader()).not.toHaveTextContent(/Showing/);
    expect(errors).not.toHaveBeenCalled();
  });

  it("fires for a post whose feed text is long", async () => {
    const { calls } = setup({
      ...items(post(1, { read: true, is_summary: false, content_html: `<p>${"word ".repeat(600)}</p>` })),
      "GET /api/rss/posts/1/article": { markdown: "Extracted" },
    });
    await userEvent.click(await option(/Post 1/));
    await waitFor(() => expect(gets(calls, "/api/rss/posts/1/article")).toHaveLength(1));
  });
});
