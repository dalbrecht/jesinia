// SPDX-License-Identifier: AGPL-3.0-or-later
// Workspace tabs (design pass pattern 1): the queue, then one tab per opened review.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { TABS_KEY } from "../tabs";
import { prView, queue, routes, row } from "./fixtures";

const FIRST = row(1, { title: "billing: prorate", recommendation: "needs_closer_look" });
const ROWS = [FIRST, row(2, { title: "auth: rotate keys", recommendation: "request_changes" })];

function setup(over: Routes = {}) {
  const env = fakeClient(routes(queue(ROWS), over));
  render(<App client={env.client} />);
  return env;
}

const workspace = () => screen.getByRole("navigation", { name: "Open reviews" });
const openCard = async (name: RegExp) => {
  const c = await screen.findByRole("article", { name });
  await userEvent.click(within(c).getByRole("button", { name: /^Open / }));
};

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/review/");
});
afterEach(() => vi.restoreAllMocks());

describe("workspace tabs", () => {
  it("opening a card opens its tab with a verdict dot; closing goes back to the queue", async () => {
    setup();
    await openCard(/#1/);
    const tab = within(workspace()).getByRole("link", { name: /Acme\/web#1 billing: prorate/ });
    expect(tab).toHaveAttribute("aria-current", "page");
    expect(tab).toHaveAttribute("href", "/review/acme/web/1");
    expect(within(tab).getByTitle("Needs a closer look")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/review/acme/web/1");
    expect(await screen.findByRole("heading", { name: "billing: prorate" })).toBeInTheDocument();
    await userEvent.click(within(workspace()).getByRole("button", { name: "Close Acme/web#1" }));
    expect(within(workspace()).queryByRole("link", { name: /Acme\/web#1/ })).toBeNull();
    expect(window.location.pathname).toBe("/review/");
    expect(within(workspace()).getByRole("link", { name: /Queue/ })).toHaveAttribute("aria-current", "page");
  });

  it("opening an open PR again focuses its tab; closing another tab keeps the current one", async () => {
    setup();
    await openCard(/#1/);
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    await openCard(/#2/);
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    await openCard(/#1/);
    expect(within(workspace()).getAllByRole("link", { name: /Acme\/web#1/ })).toHaveLength(1);
    await userEvent.click(within(workspace()).getByRole("button", { name: "Close Acme/web#2" }));
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#1/ })).toHaveAttribute("aria-current", "page");
  });

  it("open tabs persist and come back, without loading them again", async () => {
    const { calls } = setup();
    await openCard(/#2/);
    expect(JSON.parse(localStorage.getItem(TABS_KEY) ?? "[]")).toEqual([
      { repo: "acme/web", number: 2, label: "Acme/web#2 auth: rotate keys", verdict: "request_changes" },
    ]);
    cleanup();
    // A fresh launch of the app brings the tab back, read from the cache only.
    window.history.replaceState(null, "", "/review/");
    const again = fakeClient(routes(queue(ROWS)));
    render(<App client={again.client} />);
    expect(await within(workspace()).findByRole("link", { name: /Acme\/web#2 auth: rotate keys/ })).toBeInTheDocument();
    await waitFor(() => expect(again.calls.some((c) => new URL(c.url).pathname === "/api/review/queue")).toBe(true));
    // Opening the tab asked the server to start its review (POST /open); nothing was loaded,
    // and the restored (not yet shown) tab wrote nothing at all.
    const writes = (list: Request[]) => list.filter((c) => c.method !== "GET").map((c) => new URL(c.url).pathname);
    expect(writes(calls)).toEqual(["/api/review/prs/acme/web/2/open"]);
    expect(writes(again.calls)).toHaveLength(0);
  });

  it("restores saved tabs on start and ignores a corrupt or unreadable store", async () => {
    localStorage.setItem(
      TABS_KEY,
      JSON.stringify([
        { repo: "acme/web", number: 2, label: "Acme/web#2 auth: rotate keys", verdict: null },
        { bad: 1 },
      ]),
    );
    setup();
    const restored = await within(workspace()).findByRole("link", { name: /Acme\/web#2/ });
    // The saved tab follows its row once the queue is read: its dot now gives the verdict.
    expect(await within(restored).findByTitle("Request changes")).toBeInTheDocument();
    localStorage.setItem(TABS_KEY, "{not json");
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    cleanup();
    const second = fakeClient(routes(queue(ROWS)));
    render(<App client={second.client} />);
    expect(getItem).toHaveBeenCalled();
    // Storage refused: the queue still renders, with only the Queue tab.
    expect(await screen.findByRole("article", { name: /#1/ })).toBeInTheDocument();
    expect(within(workspace()).getAllByRole("link")).toHaveLength(1);
    await userEvent.click(within(screen.getByRole("article", { name: /#1/ })).getByRole("button", { name: /^Open / }));
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#1/ })).toHaveAttribute("aria-current", "page");
  });

  it("the browser's back and forward move between the queue and tabs", async () => {
    setup();
    await openCard(/#1/);
    act(() => {
      window.history.pushState(null, "", "/review/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(within(workspace()).getByRole("link", { name: /Queue/ })).toHaveAttribute("aria-current", "page");
    act(() => {
      window.history.pushState(null, "", "/review/acme/web/1");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#1/ })).toHaveAttribute("aria-current", "page");
  });
});

describe("focus", () => {
  it("moves to the opened tab, then to its neighbour (or the Queue tab) on close", async () => {
    setup();
    await openCard(/#1/);
    const first = within(workspace()).getByRole("link", { name: /Acme\/web#1/ });
    await waitFor(() => expect(first).toHaveFocus());
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    await openCard(/#2/);
    await waitFor(() => expect(within(workspace()).getByRole("link", { name: /Acme\/web#2/ })).toHaveFocus());
    await userEvent.click(within(workspace()).getByRole("button", { name: "Close Acme/web#2" }));
    await waitFor(() => expect(within(workspace()).getByRole("link", { name: /Acme\/web#1/ })).toHaveFocus());
    await userEvent.click(within(workspace()).getByRole("button", { name: "Close Acme/web#1" }));
    await waitFor(() => expect(within(workspace()).getByRole("link", { name: "Queue" })).toHaveFocus());
  });
});

describe("an opened review", () => {
  it("is marked stale while the PR has moved past the reviewed head", async () => {
    setup({ "GET /api/review/prs/acme/web/1": () => Response.json(prView(FIRST, { stale: true })) });
    await openCard(/#1/);
    expect(await screen.findByText(/Stale/)).toBeInTheDocument();
    expect(screen.getByText(/newer commits than this review/)).toBeInTheDocument();
  });

  it("shows its cached summary and a link to GitHub", async () => {
    setup();
    await openCard(/#1/);
    await screen.findByRole("heading", { name: "billing: prorate" });
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/Acme/web/pull/1",
    );
  });

  it("says why it couldn't be read", async () => {
    setup({
      "GET /api/review/prs/acme/web/1": () =>
        Response.json({ error: "ReviewError", detail: "review.sqlite is locked" }, { status: 500 }),
    });
    await openCard(/#1/);
    expect(await screen.findByText(/review.sqlite is locked/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument());
  });
});
