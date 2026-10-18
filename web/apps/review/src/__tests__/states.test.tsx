// SPDX-License-Identifier: AGPL-3.0-or-later
// The queue's less common states: every pre-review state's words, narrow windows, failures of
// each action, and the freshness line.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, freshness } from "../App";
import type { Queue } from "../api";
import { TABS_KEY } from "../tabs";
import { prView, queue, routes, row } from "./fixtures";

function setup(q: Queue | (() => Queue), over: Routes = {}) {
  const env = fakeClient(routes(q, over));
  render(<App client={env.client} />);
  return env;
}

const sections = () => screen.getByRole("tablist", { name: "Queue sections" });
const section = (name: RegExp) => within(sections()).getByRole("tab", { name });
const card = (name: RegExp) => screen.getByRole("article", { name });
const workspace = () => screen.getByRole("navigation", { name: "Open reviews" });
const toast = async (text: RegExp) => expect((await screen.findAllByText(text)).length).toBeGreaterThan(0);
const down = () => {
  throw new TypeError("network down");
};

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
  window.history.replaceState(null, "", "/review/");
});
afterEach(() => {
  vi.stubGlobal("matchMedia", undefined); // jsdom has none: the wide layout
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** Moves the app's polling clock on: polls wait seconds, and real waits made CI time out. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("pre-review states", () => {
  it("say what each row waits on, in words, and the strip counts them", async () => {
    setup(
      queue([
        row(1, { prereview_state: "waiting_auto_review", auto_review_state: "pending", bot_review_count: 0 }),
        row(2, { prereview_state: "debouncing", auto_review_state: "not_configured", bot_review_count: 0 }),
        row(3, { prereview_state: "queued", review_requested: false }),
        row(4, { prereview_state: "running", evidence: { ci: "passing", tests: null, findings: {} } }),
        row(5, { prereview_state: "not_started", ci_state: "pending", coverage_pct: null }),
        row(6, { ci_state: "none", fetch_error: "HTTP 404", prereview_state: "waiting_ci" }),
        row(7, { suggested: true, prereview_state: "queued" }),
      ]),
    );
    await waitFor(() => expect(section(/Waiting/)).toHaveTextContent("7"));
    expect(section(/Ready for you/)).toHaveAttribute("aria-selected", "false");
    expect(card(/#1/)).toHaveTextContent("Waiting for automated review");
    expect(card(/#1/)).toHaveTextContent("Automated review pending");
    expect(card(/#2/)).toHaveTextContent("Waiting for pushes to settle");
    expect(card(/#2/)).not.toHaveTextContent("automated review");
    expect(card(/#3/)).toHaveTextContent("Pre-review queued");
    expect(card(/#3/)).not.toHaveTextContent("Review requested");
    expect(card(/#4/)).toHaveTextContent("Pre-review running");
    expect(card(/#4/)).toHaveTextContent("CI passing");
    expect(card(/#5/)).toHaveTextContent("Runs when you open it");
    expect(card(/#5/)).toHaveTextContent("CI running");
    expect(card(/#6/)).toHaveTextContent("Couldn't load: HTTP 404");
    expect(card(/#6/)).toHaveTextContent("No CI");
    expect(card(/#7/)).not.toHaveTextContent(/queued/i);
    await userEvent.click(section(/Ready for you/));
    expect(screen.getByText("Nothing in Ready for you.")).toBeInTheDocument();
    const strip = screen.getByRole("list", { name: "Other sections" });
    expect(strip).toHaveTextContent("1 on automated review");
    expect(strip).toHaveTextContent("1 couldn't load");
    expect(strip).toHaveTextContent("1 from suggested repos");
    expect(strip).toHaveTextContent("Loaded · 0");
    expect(strip).toHaveTextContent("Nothing here");
  });

  it("the strip opens the section it names, and lists the loaded PRs", async () => {
    setup(
      queue([row(1), row(2, { loaded: true }), row(3, { loaded: true }), row(4, { loaded: true, kind: "rebase" })]),
    );
    await screen.findByRole("article", { name: /#1/ });
    const strip = screen.getByRole("list", { name: "Other sections" });
    expect(strip).toHaveTextContent("Acme/web#2, Acme/web#3 +1 more");
    await userEvent.click(within(strip).getByRole("button", { name: /Loaded · 3/ }));
    expect(section(/Loaded/)).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("list", { name: "Other sections" })).toHaveTextContent("1 quick · 0 deep");
    await userEvent.click(screen.getByRole("button", { name: "Open next" }));
    expect(within(workspace()).getByRole("link", { name: /Acme\/web#2/ })).toHaveAttribute("aria-current", "page");
  });

  it("Still approved can be hidden again, and one PR reads as one", async () => {
    setup(queue([row(1, { kind: "rebase" })]));
    await waitFor(() => expect(section(/Still approved/)).toHaveAttribute("aria-selected", "true"));
    await userEvent.click(screen.getByRole("button", { name: "Show 1 still approved PR" }));
    await userEvent.click(screen.getByRole("button", { name: "Hide 1 still approved PR" }));
    expect(screen.queryByRole("article", { name: /#1/ })).toBeNull();
  });

  it("a loaded PR held by CI offers Review anyway; an answer that holds it says why", async () => {
    setup(queue([row(1, { loaded: true, prereview_state: "waiting_ci", ci_state: "pending" })]), {
      "POST /api/review/prs/acme/web/1/review-anyway": {
        state: "ci_failing",
        reason: "CI is failing on the head",
        round_id: null,
        jobs: [],
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Review Acme/web#1 anyway" }));
    await toast(/CI is failing on the head/);
  });
});

describe("failures", () => {
  it("each action says why it failed", async () => {
    setup(
      queue(
        [
          row(1, { prereview_state: "ci_failing", ci_state: "failing", prereview_reason: "" }),
          row(2, { prereview_state: "failed", prereview_reason: "" }),
        ],
        { suggested_repos: ["stranger/lib"] },
      ),
      {
        "POST /api/review/prs/acme/web/1/review-anyway": down,
        "POST /api/review/prs/acme/web/2/retry": down,
        "POST /api/review/repos/stranger/lib/confirm": down,
      },
    );
    await screen.findByRole("article", { name: /#1/ });
    await userEvent.click(screen.getByRole("button", { name: "Review Acme/web#1 anyway" }));
    await toast(/Couldn't review Acme\/web#1 anyway/);
    await userEvent.click(screen.getByRole("button", { name: "Retry Acme/web#2" }));
    await toast(/Couldn't retry Acme\/web#2/);
    await userEvent.click(screen.getByRole("button", { name: "Confirm stranger/lib" }));
    await toast(/Couldn't confirm stranger\/lib/);
  });

  it("a queue that can't be read at all says so, then shows it once it can", async () => {
    let fail = true;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    setup(queue([]), {
      "GET /api/review/queue": () =>
        fail
          ? Response.json({ error: "ReviewError", detail: "review.sqlite is locked" }, { status: 500 })
          : Response.json(queue([row(1)])),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't read the queue: review.sqlite is locked.");
    fail = false;
    await advance(2100); // the first retry after a failed read
    expect(await screen.findByRole("article", { name: /#1/ })).toBeInTheDocument();
  });

  it("⌘K says why a load failed", async () => {
    setup(queue([row(1)]), {
      "POST /api/review/load": () =>
        Response.json({ error: "PrNotOpenError", detail: "acme/web#9 is merged" }, { status: 409 }),
    });
    await screen.findByRole("article", { name: /#1/ });
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("combobox"), "acme/web#9");
    await userEvent.click(await within(dialog).findByRole("option", { name: /Load acme\/web#9/ }));
    await toast(/Couldn't load acme\/web#9: acme\/web#9 is merged/);
  });

  it("an opened review that couldn't be read tries again", async () => {
    let fail = true;
    setup(queue([row(1)]), {
      "GET /api/review/prs/acme/web/1": () =>
        fail
          ? Response.json({ error: "ReviewError", detail: "locked" }, { status: 500 })
          : Response.json(prView(row(1), { round: { id: 3, head_sha: "a".repeat(40), base_sha: "b".repeat(40), depth: "quick", started_at: "2026-10-17T10:00:00Z", finished_at: null, verdict: null, error: "the agent timed out", posted_review_url: null, done: false } })),
    }); // prettier-ignore
    await userEvent.click(
      within(await screen.findByRole("article", { name: /#1/ })).getByRole("button", { name: /^Open / }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(screen.queryByText(/The last review failed/)).toBeNull();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("The last review failed: the agent timed out")).toBeInTheDocument();
  });
});

describe("narrow windows", () => {
  it("the repo filter is a select, and the layout follows the window", async () => {
    const narrow = stubNarrow(true);
    setup(queue([row(1), row(2, { repo: "acme/api", display_repo: "Acme/api" })], { suggested_repos: ["x/y"] }));
    const select = await screen.findByRole("combobox", { name: /Repo/ });
    expect(screen.queryByRole("navigation", { name: "Repos" })).toBeNull();
    expect(screen.getByRole("group", { name: "Suggested repos" })).toBeInTheDocument();
    await userEvent.selectOptions(select, "acme/api");
    expect(screen.queryByRole("article", { name: /Acme\/web#1/ })).toBeNull();
    expect(screen.getByText("Acme/api")).toBeInTheDocument();
    await userEvent.selectOptions(select, "");
    expect(screen.getByRole("article", { name: /Acme\/web#1/ })).toBeInTheDocument();
    narrow.set(false);
    expect(await screen.findByRole("navigation", { name: "Repos" })).toBeInTheDocument();
  });
});

describe("workspace tab links", () => {
  it("a plain click stays in the app; a modified click is the browser's", async () => {
    localStorage.setItem(
      TABS_KEY,
      JSON.stringify([{ repo: "acme/web", number: 1, label: "Acme/web#1 PR 1", verdict: "approve" }]),
    );
    setup(queue([row(1)]));
    const tab = await within(workspace()).findByRole("link", { name: /Acme\/web#1/ });
    expect(within(tab).getByTitle("Approve")).toBeInTheDocument();
    fireEvent.click(tab, { metaKey: true });
    expect(window.location.pathname).toBe("/review/");
    await userEvent.click(tab);
    expect(window.location.pathname).toBe("/review/acme/web/1");
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    expect(window.location.pathname).toBe("/review/");
  });

  it("saved tabs that aren't tabs are dropped", async () => {
    localStorage.setItem(
      TABS_KEY,
      JSON.stringify([
        null,
        3,
        { repo: "a/b", number: 1, label: "x", verdict: 4 },
        { repo: "a/b", number: 2, label: "a/b#2", verdict: null },
      ]),
    );
    setup(queue([]));
    expect(await within(workspace()).findByRole("link", { name: /a\/b#2/ })).toBeInTheDocument();
    expect(within(workspace()).getAllByRole("link")).toHaveLength(2);
  });

  it("a saved store that isn't a list is ignored", async () => {
    localStorage.setItem(TABS_KEY, JSON.stringify({ tabs: [] }));
    setup(queue([]));
    await screen.findByText(/Nothing needs your review/);
    expect(within(workspace()).getAllByRole("link")).toHaveLength(1);
  });
});

describe("polling", () => {
  const gets = (calls: Request[]) =>
    calls.filter((c) => c.method === "GET" && new URL(c.url).pathname === "/api/review/queue").length;

  it("relaxes to the idle pace while GitHub auth is missing, even with nothing cached", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup(queue([], { synced_at: null, sync_error: "no GitHub token: run `gh auth login`" }));
    await screen.findByText(/Nothing is cached yet/);
    await advance(2600);
    expect(gets(calls)).toBe(1);
  });

  it("backs off when the queue can't be read again and again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup(queue([]), {
      "GET /api/review/queue": () => Response.json({ error: "ReviewError", detail: "locked" }, { status: 500 }),
    });
    await screen.findByRole("alert");
    // Retries at 2 s, then 4 s later: by 5 s only the first retry has run (a steady 2 s would be 2).
    await advance(5000);
    expect(gets(calls)).toBe(2);
  });

  it("stops polling once the app is gone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const env = fakeClient(routes(queue([], { synced_at: null, syncing: true })));
    const view = render(<App client={env.client} />);
    await screen.findByText(/first time/);
    view.unmount();
    const before = gets(env.calls);
    await advance(2500);
    expect(gets(env.calls)).toBe(before);
  });
});

describe("freshness", () => {
  it("reads Not updated yet, minutes ago, or the time", () => {
    const now = new Date("2026-10-17T12:00:00Z");
    expect(freshness(queue([], { synced_at: null, syncing: false }), now)).toBe("Not updated yet");
    expect(freshness(queue([], { synced_at: "2026-10-17T11:59:50Z", syncing: true }), now)).toBe(
      "Updated just now · refreshing…",
    );
    expect(freshness(queue([], { synced_at: "2026-10-17T11:15:00Z" }), now)).toBe("Updated 45 min ago");
    expect(freshness(queue([], { synced_at: "2026-10-17T09:00:00Z" }), now)).toBe("Updated 3 h ago");
    expect(freshness(queue([], { synced_at: "2026-10-16T11:00:00Z" }), now)).toBe("Updated 1 day ago");
    expect(freshness(queue([], { synced_at: "2026-10-14T12:00:00Z" }), now)).toBe("Updated 3 days ago");
    // Older than a week: the date, never a bare clock time that reads as today.
    expect(freshness(queue([], { synced_at: "2026-10-01T12:00:00Z" }), now)).toMatch(/^Updated on .*2026/);
    expect(freshness(queue([], { synced_at: "not a date" }), now)).toBe("Not updated yet");
  });

  it("a failed sync after a good one keeps the last good time beside the banner (R56 is backend)", async () => {
    setup(
      queue([row(1)], {
        synced_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
        sync_error: "github.com unreachable",
      }),
    );
    expect(await screen.findByText("Updated 3 days ago")).toBeInTheDocument();
    expect(screen.getByText(/Couldn't refresh from GitHub/)).toBeInTheDocument();
    expect(screen.queryByText("Not updated yet")).toBeNull();
  });
});
