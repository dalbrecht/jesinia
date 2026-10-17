// SPDX-License-Identifier: AGPL-3.0-or-later
// Codex round 2 on PR #58: open once per head per tab (P1, amending R60), Done names its round
// (P2), and seeds that didn't fit under the draft limit are reported (P2).
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { queue, routes } from "./fixtures";
import { API, drafts, NEWER, PATH, REVIEWED, reviewed, ROUND } from "./pr-fixtures";

class FakeSource {
  static all: FakeSource[] = [];
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener() {}
  close() {}
}

function setup(view: () => PrView, over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([REVIEWED]), {
      [`GET ${API}`]: () => Response.json(view()),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const refresh = async () => act(async () => FakeSource.all.find((s) => s.url.includes("review.progress"))?.onopen?.());

beforeEach(() => {
  localStorage.clear();
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("open once per head per tab (P1)", () => {
  it("a refreshed view on a new head opens once more; refreshes on the same head open nothing", async () => {
    let head = REVIEWED.head_sha;
    const { calls } = setup(() => reviewed({}, { head_sha: head }));
    await ready();
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`)).toHaveLength(1));
    await refresh();
    await refresh();
    await act(async () => new Promise((r) => setTimeout(r, 20)));
    expect(sent(calls, "POST", `${API}/open`)).toHaveLength(1);
    head = NEWER;
    await refresh();
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`)).toHaveLength(2));
    await refresh();
    await act(async () => new Promise((r) => setTimeout(r, 20)));
    expect(sent(calls, "POST", `${API}/open`)).toHaveLength(2);
  });
});

describe("Done names its round (P2)", () => {
  it("sends the round shown, and a newer round's refusal says so", async () => {
    const { calls } = setup(() => reviewed(), {
      [`POST ${API}/done`]: () =>
        Response.json(
          { error: "RoundChanged", detail: "a new review round (4) started since round 3 was shown" },
          { status: 409 },
        ),
    });
    await ready();
    await userEvent.click(within(outline()).getByRole("link", { name: /Recommendation/ }));
    await userEvent.click(screen.getByRole("button", { name: "Done without posting" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/done`)).toHaveLength(1));
    expect(await sent(calls, "POST", `${API}/done`)[0]?.clone().json()).toEqual({ round_id: ROUND.id });
    expect((await screen.findAllByText(/A new review round started: it wasn't marked done/)).length).toBeGreaterThan(0);
  });
});

describe("seeds past the draft limit (P2)", () => {
  it("says how many finding drafts didn't fit", async () => {
    setup(() => reviewed(), {
      [`PUT ${API}/drafts`]: async (req: Request) => {
        const body = (await req.clone().json()) as ReturnType<typeof drafts>;
        return Response.json({ ...drafts({ summary: body.summary, drafts: body.drafts }), seeds_dropped: 50 });
      },
    });
    await ready();
    await userEvent.click(within(outline()).getByRole("link", { name: /Your comments/ }));
    const box = screen.getByRole("textbox", { name: "Review summary" });
    await userEvent.click(box);
    await userEvent.paste(" more");
    fireEvent.blur(box);
    expect(
      await screen.findByText("50 finding drafts didn't fit: a review takes at most 500 drafts, and yours come first."),
    ).toBeInTheDocument();
  });
});
