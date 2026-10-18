// SPDX-License-Identifier: AGPL-3.0-or-later
// Load a PR (spec §2): the Load PR field, ⌘K, and opening /review/<owner>/<repo>/<n>.
import { fakeClient, type Routes } from "@jesinia/shell";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { prView, queue, routes, row } from "./fixtures";

const LOADED = row(7, { title: "billing: prorate", loaded: true });

function setup(over: Routes = {}) {
  const env = fakeClient(
    routes(queue([row(1)]), {
      "POST /api/review/load": () => Response.json(LOADED),
      "GET /api/review/prs/acme/web/7": () => Response.json(prView(LOADED)),
      "POST /api/review/prs/acme/web/7/open": () =>
        Response.json({ state: "up_to_date", reason: "complete", round_id: null, jobs: [] }),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const loads = (calls: Request[]) =>
  calls.filter((c) => c.method === "POST" && new URL(c.url).pathname === "/api/review/load");
const field = () => screen.getByRole("textbox", { name: "Load a PR" });
const workspace = () => screen.getByRole("navigation", { name: "Open reviews" });

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/review/");
});
afterEach(() => vi.restoreAllMocks());

describe("Load PR", () => {
  it.each([["https://github.com/acme/web/pull/7"], ["acme/web#7"]])("loads %s and opens it", async (ref) => {
    const { calls } = setup();
    await screen.findByRole("article", { name: /#1/ });
    await userEvent.type(field(), ref);
    await userEvent.click(screen.getByRole("button", { name: "Load" }));
    await waitFor(() => expect(window.location.pathname).toBe("/review/acme/web/7"));
    const sent = loads(calls)[0];
    expect(await sent?.clone().json()).toEqual({ ref });
    expect(sent?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#7/ })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { name: "billing: prorate" })).toBeInTheDocument();
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    expect(field()).toHaveValue("");
  });

  it("says why a load failed; the retry of the same ref reuses its key", async () => {
    let attempt = 0;
    const { calls } = setup({
      "POST /api/review/load": () => {
        attempt += 1;
        if (attempt === 1)
          return Response.json({ error: "GitHubUnavailable", detail: "GitHub is down" }, { status: 503 });
        if (attempt === 2) return Response.json(LOADED);
        return Response.json({ error: "InvalidPrRefError", detail: "not a PR: nonsense" }, { status: 400 });
      },
    });
    await screen.findByRole("article", { name: /#1/ });
    await userEvent.type(field(), "acme/web#7");
    await userEvent.click(screen.getByRole("button", { name: "Load" }));
    expect(await screen.findByText("GitHub is down.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load" }));
    await waitFor(() => expect(window.location.pathname).toBe("/review/acme/web/7"));
    const [first, second] = loads(calls).map((r) => r.headers.get("Idempotency-Key"));
    expect(second).toBe(first);
    // A new action gets a new key, and a 400 is shown in the field.
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    await userEvent.type(field(), "nonsense");
    fireEvent.submit(field());
    expect(await screen.findByText("not a PR: nonsense.")).toBeInTheDocument();
    expect(loads(calls)[2]?.headers.get("Idempotency-Key")).not.toBe(first);
  });

  it("an empty field does nothing", async () => {
    const { calls } = setup();
    await screen.findByRole("article", { name: /#1/ });
    fireEvent.submit(field());
    expect(loads(calls)).toHaveLength(0);
  });

  it("⌘K offers Load for a pasted PR and Open for queue rows", async () => {
    const { calls } = setup();
    await screen.findByRole("article", { name: /#1/ });
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("combobox"), "acme/web#7");
    await userEvent.click(await within(dialog).findByRole("option", { name: /Load acme\/web#7/ }));
    await waitFor(() => expect(window.location.pathname).toBe("/review/acme/web/7"));
    expect(loads(calls)).toHaveLength(1);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const again = await screen.findByRole("dialog");
    await userEvent.type(within(again).getByRole("combobox"), "PR 1");
    await userEvent.click(await within(again).findByRole("option", { name: /Open Acme\/web#1/ }));
    await waitFor(() => expect(window.location.pathname).toBe("/review/acme/web/1"));
  });
});

describe("opening /review/<owner>/<repo>/<n>", () => {
  it("a cached PR opens without being loaded again (M4: a reload never re-runs Load)", async () => {
    window.history.replaceState(null, "", "/review/acme/web/7");
    const { calls } = setup();
    expect(await screen.findByRole("heading", { name: "billing: prorate" })).toBeInTheDocument();
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#7/ })).toHaveAttribute("aria-current", "page");
    const opens = () => calls.filter((c) => c.method === "POST" && new URL(c.url).pathname.endsWith("/7/open"));
    await waitFor(() => expect(opens()).toHaveLength(1));
    expect(loads(calls)).toHaveLength(0);
  });

  it("a PR that isn't cached is loaded, then opened", async () => {
    window.history.replaceState(null, "", "/review/acme/web/7");
    let cached = false;
    const { calls } = setup({
      "POST /api/review/load": () => {
        cached = true;
        return Response.json(LOADED);
      },
      "GET /api/review/prs/acme/web/7": () =>
        cached
          ? Response.json(prView(LOADED))
          : Response.json({ error: "PrNotInQueueError", detail: "not in the review queue" }, { status: 404 }),
      "POST /api/review/prs/acme/web/7/open": () =>
        Response.json({ state: "up_to_date", reason: "complete", round_id: null, jobs: [] }),
    });
    expect(await screen.findByRole("heading", { name: "billing: prorate" })).toBeInTheDocument();
    expect(await loads(calls)[0]?.clone().json()).toEqual({ ref: "/review/acme/web/7" });
    expect(loads(calls)).toHaveLength(1);
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#7/ })).toHaveAttribute("aria-current", "page");
  });

  it("says why a linked PR couldn't be loaded, in its tab", async () => {
    window.history.replaceState(null, "", "/review/acme/web/7");
    setup({
      "POST /api/review/load": () =>
        Response.json({ error: "PrNotOpenError", detail: "acme/web#7 is merged" }, { status: 409 }),
      "GET /api/review/prs/acme/web/7": () =>
        Response.json({ error: "PrNotInQueueError", detail: "acme/web#7 is not in the review queue" }, { status: 404 }),
    });
    expect((await screen.findAllByText(/acme\/web#7 is merged/)).length).toBeGreaterThan(0);
  });
});
