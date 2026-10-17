// SPDX-License-Identifier: AGPL-3.0-or-later
// The queue (spec §2): sections as tabs with counts, cards with every field, always cached.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Queue } from "../api";
import { AUTH_MISSING, queue, routes, row } from "./fixtures";

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {}
}

function setup(q: Queue | (() => Queue), over: Routes = {}) {
  const env = fakeClient(routes(q, over));
  render(<App client={env.client} />);
  return env;
}

const sections = () => screen.getByRole("tablist", { name: "Queue sections" });
const section = (name: RegExp) => within(sections()).getByRole("tab", { name });
const card = (name: RegExp) => screen.getByRole("article", { name });
const writes = (calls: Request[], path: string) =>
  calls.filter((c) => c.method !== "GET" && new URL(c.url).pathname === path);
const gets = (calls: Request[], path: string) =>
  calls.filter((c) => c.method === "GET" && new URL(c.url).pathname === path);

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/review/");
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("queue rows", () => {
  it("show every spec §2 field", async () => {
    setup(queue([row(57, { title: "notes: tag chips", author_kind: "agent", author: "claude-code" })]));
    const c = await screen.findByRole("article", { name: /Acme\/web#57/ });
    expect(c).toHaveTextContent("Acme/web#57");
    expect(c).toHaveTextContent("notes: tag chips");
    expect(c).toHaveTextContent("claude-code (agent)");
    expect(c).toHaveTextContent("382 lines · 9 files");
    expect(c).toHaveTextContent("3 days old");
    expect(c).toHaveTextContent("New");
    expect(c).toHaveTextContent("Quick");
    expect(c).toHaveTextContent("CI passing");
    expect(c).toHaveTextContent("1 automated review");
    expect(c).toHaveTextContent("2 human reviews");
    expect(c).toHaveTextContent("4 review comments");
    expect(c).toHaveTextContent("Review requested");
    expect(c).toHaveTextContent("Pre-review ready");
    expect(c).toHaveTextContent("✓ Approve");
    expect(c).toHaveTextContent("tests 12 passed");
    expect(c).toHaveTextContent("0 important · 2 minor");
    expect(c).toHaveTextContent("walk 88%");
  });

  it("say Nothing to walk, never 100%, when the diff changes no line (M3)", async () => {
    setup(queue([row(3, { coverage_pct: 100, nothing_to_walk: true })]));
    const c = await screen.findByRole("article", { name: /#3/ });
    expect(c).toHaveTextContent("Nothing to walk");
    expect(c).not.toHaveTextContent("100%");
  });

  it("render PR text only as text, never as markup", async () => {
    const evil = '<img src=x onerror="alert(1)"> <b>bold</b>';
    setup(queue([row(4, { title: evil, author: "<script>x</script>" })]));
    const c = await screen.findByRole("article", { name: /#4/ });
    expect(c).toHaveTextContent(evil);
    expect(c).toHaveTextContent("<script>x</script>");
    expect(c.querySelector("img, b, script")).toBeNull();
  });

  it("mark a review of an older head stale", async () => {
    setup(queue([row(5, { recommendation_stale: true })]));
    expect(await screen.findByRole("article", { name: /#5/ })).toHaveTextContent("stale");
  });

  it("dismiss sends a keyed write and the row leaves; a retry after no answer reuses the key", async () => {
    let fail = true;
    let dismissed = false;
    const { calls } = setup(() => queue(dismissed ? [row(7)] : [row(6), row(7)]), {
      "POST /api/review/queue/acme/web/6/dismiss": () => {
        if (fail) {
          fail = false;
          throw new TypeError("network down");
        }
        dismissed = true;
        return new Response(null, { status: 204 });
      },
    });
    await screen.findByRole("article", { name: /#6/ });
    await userEvent.click(within(card(/#6/)).getByRole("button", { name: "Dismiss Acme/web#6" }));
    expect((await screen.findAllByText(/Couldn't dismiss Acme\/web#6/)).length).toBeGreaterThan(0);
    await userEvent.click(within(card(/#6/)).getByRole("button", { name: "Dismiss Acme/web#6" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: /#6/ })).toBeNull());
    const sent = writes(calls, "/api/review/queue/acme/web/6/dismiss");
    expect(sent).toHaveLength(2);
    const [first, second] = sent.map((r) => r.headers.get("Idempotency-Key"));
    expect(first).toBeTruthy();
    expect(second).toBe(first);
  });
});

describe("sections", () => {
  const mixed = () =>
    queue([
      row(1),
      row(2, { depth: "deep" }),
      row(3, { prereview_state: "waiting_ci", ci_state: "pending", recommendation: null, evidence: null }),
      row(4, { prereview_state: "ci_failing", ci_state: "failing", prereview_reason: "CI is failing on the head" }),
      row(5, { loaded: true }),
      row(6, { kind: "rebase" }),
      row(7, { kind: "rebase" }),
    ]);

  it("are tabs with counts, Ready for you first, and a summary strip shows the others", async () => {
    setup(mixed);
    await screen.findByRole("article", { name: /#1/ });
    expect(section(/Ready for you/)).toHaveTextContent("2");
    expect(section(/Waiting/)).toHaveTextContent("2");
    expect(section(/Loaded/)).toHaveTextContent("1");
    expect(section(/Still approved/)).toHaveTextContent("2");
    expect(section(/Ready for you/)).toHaveAttribute("aria-selected", "true");
    const strip = screen.getByRole("list", { name: "Other sections" });
    expect(strip).toHaveTextContent("Waiting · 2");
    expect(strip).toHaveTextContent("1 waiting for CI · 1 CI failing");
    expect(strip).toHaveTextContent("Loaded · 1");
    expect(strip).toHaveTextContent("Still approved · 2");
    expect(screen.getByText(/Pre-reviewed with CI passing/)).toBeInTheDocument();
  });

  it("Waiting shows waiting for CI, and a failing CI offers Review anyway (keyed)", async () => {
    const { calls } = setup(mixed, {
      "POST /api/review/prs/acme/web/4/review-anyway": () =>
        Response.json({ state: "started", reason: "", round_id: 9, jobs: ["recommend"] }, { status: 202 }),
    });
    await screen.findByRole("article", { name: /#1/ });
    await userEvent.click(section(/Waiting/));
    expect(card(/#3/)).toHaveTextContent("Waiting for CI");
    expect(within(card(/#3/)).queryByRole("button", { name: /Review anyway/ })).toBeNull();
    expect(card(/#4/)).toHaveTextContent("CI failing");
    expect(card(/#4/)).toHaveTextContent("CI is failing on the head");
    await userEvent.click(within(card(/#4/)).getByRole("button", { name: "Review Acme/web#4 anyway" }));
    await waitFor(() => expect(writes(calls, "/api/review/prs/acme/web/4/review-anyway")).toHaveLength(1));
    expect(writes(calls, "/api/review/prs/acme/web/4/review-anyway")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    // It opens the PR in its own workspace tab.
    expect(await screen.findByRole("link", { name: /Acme\/web#4/ })).toHaveAttribute("aria-current", "page");
    expect(window.location.pathname).toBe("/review/acme/web/4");
  });

  it("Still approved is collapsed until asked", async () => {
    setup(mixed);
    await screen.findByRole("article", { name: /#1/ });
    await userEvent.click(section(/Still approved/));
    const toggle = screen.getByRole("button", { name: /Show 2 still approved/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("article", { name: /#6/ })).toBeNull();
    await userEvent.click(toggle);
    expect(screen.getByRole("article", { name: /#6/ })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: /#7/ })).toBeInTheDocument();
  });

  it("Open next and Open all quick open workspace tabs", async () => {
    setup(queue([row(1), row(2, { depth: "deep" }), row(8)]));
    await screen.findByRole("article", { name: /#1/ });
    await userEvent.click(screen.getByRole("button", { name: "Open all quick (2)" }));
    const tabs = screen.getByRole("navigation", { name: "Open reviews" });
    expect(within(tabs).getByRole("link", { name: /Acme\/web#1/ })).toBeInTheDocument();
    expect(within(tabs).getByRole("link", { name: /Acme\/web#8/ })).toBeInTheDocument();
    expect(within(tabs).queryByRole("link", { name: /Acme\/web#2/ })).toBeNull();
    await userEvent.click(within(tabs).getByRole("link", { name: /Queue/ }));
    await userEvent.click(screen.getByRole("button", { name: "Open next" }));
    expect(within(tabs).getByRole("link", { name: /Acme\/web#1/ })).toHaveAttribute("aria-current", "page");
  });

  it("a row whose fetch failed is flagged with the cause and never shown as ready", async () => {
    setup(
      queue([
        row(9, {
          prereview_state: "fetch_failed",
          fetch_error: "HTTP 502",
          prereview_reason: "couldn't load: HTTP 502",
        }),
      ]),
    );
    await waitFor(() => expect(section(/Waiting/)).toHaveTextContent("1"));
    expect(section(/Ready for you/)).toHaveTextContent("0");
    await userEvent.click(section(/Waiting/));
    const c = card(/#9/);
    expect(c).toHaveTextContent("Couldn't load: HTTP 502");
    expect(c).not.toHaveTextContent("Pre-review ready");
  });

  it("a failed pre-review shows its cause and Retry (keyed)", async () => {
    const { calls } = setup(
      queue([row(10, { prereview_state: "failed", prereview_reason: "the agent timed out", recommendation: null })]),
      {
        "POST /api/review/prs/acme/web/10/retry": () =>
          Response.json({ state: "started", reason: "", round_id: 2, jobs: [] }, { status: 202 }),
      },
    );
    await waitFor(() => expect(section(/Waiting/)).toHaveTextContent("1"));
    await userEvent.click(section(/Waiting/));
    expect(card(/#10/)).toHaveTextContent("the agent timed out");
    await userEvent.click(within(card(/#10/)).getByRole("button", { name: "Retry Acme/web#10" }));
    await waitFor(() => expect(writes(calls, "/api/review/prs/acme/web/10/retry")).toHaveLength(1));
  });
});

describe("repos", () => {
  it("the repo list filters the queue", async () => {
    setup(queue([row(1), row(2, { repo: "acme/api", display_repo: "Acme/api" })]));
    const repos = await screen.findByRole("navigation", { name: "Repos" });
    await waitFor(() => expect(within(repos).getByRole("button", { name: /All watched/ })).toHaveTextContent("2"));
    await userEvent.click(within(repos).getByRole("button", { name: /Acme\/api/ }));
    expect(screen.queryByRole("article", { name: /Acme\/web#1/ })).toBeNull();
    expect(screen.getByRole("article", { name: /Acme\/api#2/ })).toBeInTheDocument();
    expect(within(repos).getByRole("button", { name: /Acme\/api/ })).toHaveAttribute("aria-current", "true");
  });

  it("suggested repos are listed with Confirm, and their PRs never look queued (R29a)", async () => {
    let confirmed = false;
    const suggestedRow = row(11, {
      repo: "stranger/lib",
      display_repo: "stranger/lib",
      loaded: true,
      suggested: true,
      prereview_state: "queued",
    });
    const { calls } = setup(
      () =>
        queue(confirmed ? [{ ...suggestedRow, suggested: false }] : [suggestedRow], {
          suggested_repos: confirmed ? [] : ["stranger/lib"],
        }),
      {
        "POST /api/review/repos/stranger/lib/confirm": () => {
          confirmed = true;
          return new Response(null, { status: 204 });
        },
      },
    );
    const box = await screen.findByRole("group", { name: "Suggested repos" });
    await userEvent.click(section(/Loaded/));
    const c = card(/stranger\/lib#11/);
    expect(c).toHaveTextContent("Suggested repo — confirm to review automatically");
    expect(c).not.toHaveTextContent(/queued/i);
    await userEvent.click(within(box).getByRole("button", { name: "Confirm stranger/lib" }));
    await waitFor(() => expect(writes(calls, "/api/review/repos/stranger/lib/confirm")).toHaveLength(1));
    expect(writes(calls, "/api/review/repos/stranger/lib/confirm")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("group", { name: "Suggested repos" })).toBeNull());
  });
});

describe("always cached", () => {
  it("renders the cached queue at once while a sync runs, with its freshness", async () => {
    setup(queue([row(1)], { syncing: true, synced_at: new Date(Date.now() - 20 * 60_000).toISOString() }));
    expect(await screen.findByRole("article", { name: /#1/ })).toBeInTheDocument();
    expect(await screen.findByText("Updated 20 min ago · refreshing…")).toBeInTheDocument();
    expect(screen.queryByText(/first time/)).toBeNull();
  });

  it("keeps the cached queue under a banner when the sync fails", async () => {
    setup(queue([row(1)], { sync_error: "github.com unreachable" }));
    expect(await screen.findByRole("article", { name: /#1/ })).toBeInTheDocument();
    const banner = screen.getByText(/Couldn't refresh from GitHub/).closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "danger");
    expect(banner).toHaveTextContent("github.com unreachable");
  });

  it("GitHub auth missing shows the exact command over the cached queue", async () => {
    setup(queue([row(1)], { sync_error: AUTH_MISSING }));
    expect(await screen.findByRole("article", { name: /#1/ })).toBeInTheDocument();
    const banner = screen.getByText("GitHub auth missing:").closest("[data-tone]") as HTMLElement;
    expect(banner).toHaveAttribute("data-tone", "danger");
    expect(within(banner).getByText("gh auth login", { selector: "code" })).toBeInTheDocument();
    expect(within(banner).getByText("jesinia-github", { selector: "code" })).toBeInTheDocument();
  });

  it("GitHub auth missing with nothing cached is never an empty page", async () => {
    setup(queue([], { sync_error: AUTH_MISSING, synced_at: null }));
    expect(await screen.findByText("GitHub auth missing:")).toBeInTheDocument();
    expect(screen.getByText(/Nothing is cached yet/)).toBeInTheDocument();
    expect(screen.queryByText(/first time/)).toBeNull();
  });

  it("shows loading only on the very first run, then the rows as they arrive", async () => {
    let reads = 0;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup(queue([]), {
      "GET /api/review/queue": () => {
        reads += 1;
        return Response.json(reads === 1 ? queue([], { synced_at: null, syncing: true }) : queue([row(1)]));
      },
    });
    expect(await screen.findByText(/Fetching your pull requests from GitHub for the first time/)).toBeInTheDocument();
    // It polls while the first sync runs (every 2 s; the clock is moved, not waited on).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(await screen.findByRole("article", { name: /#1/ })).toBeInTheDocument();
    expect(gets(calls, "/api/review/queue").length).toBeGreaterThanOrEqual(2);
  });

  it("an empty queue says so", async () => {
    setup(queue([]));
    expect(await screen.findByText(/Nothing needs your review/)).toBeInTheDocument();
  });

  it("a queue that can't be read keeps what was shown and says why", async () => {
    let answer: Response | null = null;
    setup(() => queue([row(1)]), {
      "GET /api/review/queue": () => answer ?? Response.json(queue([row(1)])),
    });
    await screen.findByRole("article", { name: /#1/ });
    answer = Response.json({ error: "ReviewError", detail: "review.sqlite is locked" }, { status: 500 });
    await act(async () => FakeSource.all[0]?.onopen?.());
    expect((await screen.findAllByText(/review.sqlite is locked/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("article", { name: /#1/ })).toBeInTheDocument();
  });

  it("refetches on review.progress events and on every (re)connect", async () => {
    const { calls } = setup(queue([row(1)]));
    await screen.findByRole("article", { name: /#1/ });
    const source = FakeSource.all.find((s) => s.url.includes("review.progress"));
    expect(source).toBeDefined();
    const before = gets(calls, "/api/review/queue").length;
    await act(async () => source?.onopen?.());
    await waitFor(() => expect(gets(calls, "/api/review/queue").length).toBeGreaterThan(before));
    const after = gets(calls, "/api/review/queue").length;
    await act(async () =>
      source?.listeners.get("review.progress")?.(
        new MessageEvent("review.progress", {
          data: JSON.stringify({ repo: "acme/web", number: 1, state: "finished" }),
          lastEventId: "4",
        }),
      ),
    );
    await waitFor(() => expect(gets(calls, "/api/review/queue").length).toBeGreaterThan(after));
  });
});
