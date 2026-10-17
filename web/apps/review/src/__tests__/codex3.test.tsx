// SPDX-License-Identifier: AGPL-3.0-or-later
// Codex round 3 on PR #58: a new head or round on screen clears the depth override (P2),
// starts a fresh chat session with the new review (P2), and resets the quiz (P2).
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { queue, routes } from "./fixtures";
import { API, ARTIFACTS, drafts, NEWER, PATH, REVIEWED, reviewed, ROUND } from "./pr-fixtures";

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

let current: PrView;
const NEXT = () =>
  reviewed({ round: { ...ROUND, id: 4, head_sha: NEWER, depth: "quick" } }, { head_sha: NEWER, depth: "quick" });

function setup(over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([REVIEWED]), {
      [`GET ${API}`]: () => Response.json(current),
      [`GET ${API}/drafts`]: () => Response.json(drafts({ round_id: current.round?.id ?? 3 })),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const toolbar = () => screen.getByRole("group", { name: "Review tools" });
const ready = () => screen.findByRole("heading", { level: 2, name: /Overview|Explanation/ });
/** The next round arrives on a new head, as a refresh shows it. */
const nextRound = async () => {
  current = NEXT();
  await act(async () => FakeSource.all.find((s) => s.url.includes("review.progress"))?.onopen?.());
  await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument());
  await act(async () => new Promise((r) => setTimeout(r, 30)));
};

beforeEach(() => {
  localStorage.clear();
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
  current = reviewed({ round: { ...ROUND, depth: "quick" } }, { depth: "quick" });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a new head or round on screen", () => {
  it("clears the depth override", async () => {
    setup();
    await ready();
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Switch to deep" }));
    expect(within(toolbar()).getByText("Deep")).toBeInTheDocument();
    await nextRound();
    expect(within(toolbar()).getByText("Quick")).toBeInTheDocument();
  });

  it("starts a fresh chat session, so the new review is uploaded", async () => {
    let sessions = 0;
    const { calls } = setup({
      "POST /api/agents/sessions": () => {
        sessions += 1;
        return Response.json({ id: `s${sessions}` }, { status: 201 });
      },
      "POST /api/agents/sessions/s1/messages": () => Response.json({ ok: true }, { status: 202 }),
      "POST /api/agents/sessions/s2/messages": () => Response.json({ ok: true }, { status: 202 }),
    });
    await ready();
    await userEvent.click(within(toolbar()).getByRole("button", { name: /Ask/ }));
    const ask = async (text: string) => {
      const rail = await screen.findByRole("complementary", { name: "Ask about this PR" });
      await userEvent.click(within(rail).getByRole("textbox", { name: "Message" }));
      await userEvent.paste(text);
      await userEvent.click(within(rail).getByRole("button", { name: "Send" }));
    };
    await ask("First?");
    await waitFor(() => expect(sent(calls, "POST", "/api/agents/sessions/s1/messages")).toHaveLength(1));
    await nextRound();
    await ask("Second?");
    await waitFor(() => expect(sent(calls, "POST", "/api/agents/sessions")).toHaveLength(2));
    const second = (await sent(calls, "POST", "/api/agents/sessions")[1]?.clone().json()) as {
      subject: { ref: string };
    };
    expect(second.subject.ref).toBe("acme/web#3");
    await waitFor(() => expect(sent(calls, "POST", "/api/agents/sessions/s2/messages")).toHaveLength(1));
  });

  it("resets a finished quiz", async () => {
    current = reviewed();
    setup();
    await ready();
    await userEvent.click(within(outline()).getByRole("link", { name: /Quiz/ }));
    for (let i = 0; i < 3; i += 1) await userEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(screen.getByText(/0 of 3 right · 3 skipped/)).toBeInTheDocument();
    current = reviewed({ round: { ...ROUND, id: 4, head_sha: NEWER } }, { head_sha: NEWER });
    await act(async () => FakeSource.all.find((s) => s.url.includes("review.progress"))?.onopen?.());
    await act(async () => new Promise((r) => setTimeout(r, 30)));
    await userEvent.click(within(outline()).getByRole("link", { name: /Quiz/ }));
    expect(await screen.findByText("Question 1 of 3")).toBeInTheDocument();
    void ARTIFACTS;
  });
});
