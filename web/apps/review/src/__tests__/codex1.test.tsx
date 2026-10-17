// SPDX-License-Identifier: AGPL-3.0-or-later
// Codex round 1 on PR #58: saves, previews and posts are bound to the round shown (P1), and a
// deep-linked PR that has to be loaded is opened once, after the load (P2).
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { prView, queue, routes, UP_TO_DATE } from "./fixtures";
import { API, drafts, PATH, preview, REVIEWED, reviewed } from "./pr-fixtures";

function setup(view: PrView = reviewed(), over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr]), {
      [`GET ${API}`]: () => Response.json(view),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      [`POST ${API}/post/preview`]: async (req: Request) => {
        const { event } = (await req.clone().json()) as { event: "COMMENT" };
        return Response.json(preview({ event }));
      },
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const actions = () => screen.getByRole("group", { name: "Page actions" });
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const draftBox = () => screen.getByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" });
const changed = () =>
  Response.json(
    {
      error: "RoundChanged",
      detail: "a new review round (4) started since round 3 was shown; nothing was saved or posted",
    },
    { status: 409 },
  );

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("the round shown (P1)", () => {
  it("a save names its round; a newer round keeps the author's text and says so; Discard reads the new round", async () => {
    let reads = 0;
    const { calls } = setup(reviewed(), {
      [`PUT ${API}/drafts`]: changed,
      [`GET ${API}/drafts`]: () => {
        reads += 1;
        return Response.json(reads === 1 ? drafts() : drafts({ round_id: 4, summary: "New round.", drafts: [] }));
      },
    });
    await ready();
    await userEvent.click(within(outline()).getByRole("link", { name: /Your comments/ }));
    await userEvent.type(draftBox(), " mine");
    fireEvent.blur(draftBox());
    expect(
      await screen.findByText(/A new review round started; your edits are kept here — copy them or discard\./),
    ).toBeInTheDocument();
    const put = (await sent(calls, "PUT", `${API}/drafts`)[0]?.clone().json()) as { round_id: number };
    expect(put.round_id).toBe(3);
    expect(draftBox()).toHaveValue("Rounding happens once. mine"); // kept, not dropped or moved
    expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1); // no retry loop
    await userEvent.click(screen.getByRole("button", { name: "Discard my edits" }));
    expect(await screen.findByDisplayValue("New round.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" })).toBeNull();
    // The new round is clean: nothing from the old one is saved into it.
    await new Promise((r) => setTimeout(r, 20));
    expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1);
  });

  it("a preview and a post name their round; posting against a stale round is refused", async () => {
    const { calls } = setup(reviewed(), { [`POST ${API}/post`]: changed });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await screen.findByRole("dialog", { name: /on GitHub/ });
    await within(box).findByText("Looks good overall.");
    expect(await sent(calls, "POST", `${API}/post/preview`)[0]?.clone().json()).toEqual({
      event: "COMMENT",
      round_id: 3,
    });
    await userEvent.click(within(box).getByRole("button", { name: "Comment" }));
    expect(await within(box).findByText(/A new review round started: nothing was posted/)).toBeInTheDocument();
    expect(((await sent(calls, "POST", `${API}/post`)[0]?.clone().json()) as { round_id: number }).round_id).toBe(3);
    expect(within(box).getByRole("button", { name: "Comment" })).toBeDisabled();
  });
});

describe("a deep link that has to be loaded (P2)", () => {
  it("opens once, after the load", async () => {
    let loaded = false;
    let finishLoad: (() => void) | null = null;
    const order: string[] = [];
    window.history.replaceState(null, "", PATH);
    const env = fakeClient(
      routes(queue([]), {
        [`GET ${API}`]: () =>
          loaded
            ? Response.json(reviewed())
            : Response.json({ error: "PrNotInQueueError", detail: "not in the review queue" }, { status: 404 }),
        "POST /api/review/load": async () => {
          await new Promise<void>((resolve) => (finishLoad = resolve));
          loaded = true;
          order.push("load");
          return Response.json(REVIEWED);
        },
        [`POST ${API}/open`]: () => {
          order.push("open");
          return loaded
            ? Response.json(UP_TO_DATE)
            : Response.json({ error: "PrNotInQueueError", detail: "not in the review queue" }, { status: 404 });
        },
        [`GET ${API}/drafts`]: () => Response.json(drafts()),
      }),
    );
    render(<App client={env.client} />);
    await waitFor(() => expect(finishLoad).not.toBeNull());
    await new Promise((r) => setTimeout(r, 30));
    expect(order).toEqual([]); // nothing opened while the PR isn't loaded
    await act(async () => finishLoad?.());
    expect(await screen.findByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
    await waitFor(() => expect(order).toEqual(["load", "open"]));
    await new Promise((r) => setTimeout(r, 30));
    expect(order).toEqual(["load", "open"]);
    void prView;
  });
});
