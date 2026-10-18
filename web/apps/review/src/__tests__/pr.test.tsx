// SPDX-License-Identifier: AGPL-3.0-or-later
// The PR view as a book (design pass `pr-view.html`, spec §3.1): the outline on the left, the
// view pane on the right with a toolbar on top and actions along the bottom.
import { fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { queue, routes, UP_TO_DATE } from "./fixtures";
import { API, ARTIFACTS, drafts, PATH, REVIEWED, reviewed, ROUND, XSS } from "./pr-fixtures";

function setup(view: PrView = reviewed(), over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr]), {
      [`GET ${API}`]: () => Response.json(view),
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
const toolbar = () => screen.getByRole("group", { name: "Review tools" });
const actions = () => screen.getByRole("group", { name: "Page actions" });
const go = async (name: RegExp | string) => userEvent.click(within(outline()).getByRole("link", { name }));

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the book layout", () => {
  it("opens with the outline, the toolbar and the first page; opening POSTs /open once, never /load", async () => {
    const { calls } = setup();
    expect(await screen.findByRole("heading", { level: 1, name: "billing: prorate mid-cycle" })).toBeInTheDocument();
    const nav = outline();
    for (const part of ["Before you read", /Walkthrough · 82% of the change/, "After you read"]) {
      expect(within(nav).getByText(part)).toBeInTheDocument();
    }
    for (const entry of [/Overview/, /Background/, /1\. The new prorate function/, /2\. Posting to the ledger/]) {
      expect(within(nav).getByRole("link", { name: entry })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: /Not walked through.*2 files · 18%/ })).toBeInTheDocument();
    expect(await within(nav).findByRole("link", { name: /Your comments.*1 draft/ })).toBeInTheDocument();
    for (const entry of [/Findings/, /Metrics/, /Quiz.*3 questions/, /Recommendation/]) {
      expect(within(nav).getByRole("link", { name: entry })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: /2\. Posting to the ledger.*1 important/ })).toBeInTheDocument();
    expect(within(toolbar()).getByText("? Needs a closer look")).toBeInTheDocument();
    expect(within(toolbar()).getByText(/Walkthrough covers 82% · 1 of 3 files/)).toBeInTheDocument();
    expect(within(toolbar()).getByRole("link", { name: "Open on GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/Acme/web/pull/3",
    );
    // The first page is the Overview, rendered through the safe renderer.
    expect(await screen.findByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
    expect(screen.getByText("at once").tagName).toBe("STRONG");
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`)).toHaveLength(1));
    expect(sent(calls, "POST", `${API}/open`)[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(sent(calls, "POST", "/api/review/load")).toHaveLength(0);
  });

  it("walks step by step: narrative beside the diff, the step's hunk highlighted, text only", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/2\. Posting to the ledger/);
    expect(screen.getByRole("heading", { level: 2, name: "Posting to the ledger" })).toBeInTheDocument();
    expect(screen.getByText(/Rounding happens once on the difference/)).toBeInTheDocument();
    const diff = screen.getByRole("region", { name: "Diff for this step" });
    const current = within(diff).getByRole("group", { name: /src\/billing\/proration\.py hunk 2 \(this step\)/ });
    expect(within(current).getByText("@@ -10,3 +13,4 @@ def apply():")).toBeInTheDocument();
    // Every diff line is a monospace pre of text: PR content never becomes markup.
    const line = within(current).getByText(/ledger\.post\(amount\)/);
    expect(line.tagName).toBe("PRE");
    expect(line.textContent).toContain(XSS);
    expect(diff.querySelector("img")).toBeNull();
    expect(screen.getByRole("article", { name: "Posting to the ledger" }).querySelector("img")).toBeNull();
    // Agent text goes through the safe renderer: raw HTML in it is dropped, never rendered.
    expect(screen.getByText(/The change is posted\./).textContent?.trim()).toBe("The change is posted.");
    // Previous and next steps by name.
    expect(within(actions()).getByRole("button", { name: /← 1\. The new prorate function/ })).toBeInTheDocument();
    await userEvent.click(within(actions()).getByRole("button", { name: /Not walked through →/ }));
    expect(screen.getByRole("heading", { level: 2, name: "Not walked through" })).toBeInTheDocument();
  });

  it("splits the diff into old and new columns", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/1\. The new prorate function/);
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Diff: unified" }));
    expect(within(toolbar()).getByRole("button", { name: "Diff: split" })).toBeInTheDocument();
    const diff = screen.getByRole("region", { name: "Diff for this step" });
    expect(
      within(diff)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["Before", "After"]);
  });

  it("ticks what was read and counts progress in the footer", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    expect(within(outline()).getByText(/Read 0 of 10/)).toBeInTheDocument();
    await userEvent.click(within(actions()).getByRole("button", { name: "Mark read" }));
    expect(within(outline()).getByText(/Read 1 of 10/)).toBeInTheDocument();
    expect(within(outline()).getByRole("link", { name: /Overview/ })).toHaveAccessibleName(/read/);
    // Moving on is reading on: the page moved to Background.
    expect(screen.getByRole("heading", { level: 2, name: "Background" })).toBeInTheDocument();
  });
});

describe("coverage (spec §3.1)", () => {
  it("shows the coverage line and the not-walked list; Show opens one in place", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Not walked through/);
    const page = screen.getByRole("article", { name: "Not walked through" });
    expect(within(page).getByText("Walkthrough covers 82% of changed lines · 1 of 3 files")).toBeInTheDocument();
    const items = within(page).getAllByRole("listitem");
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringMatching(/poetry\.lock.*lockfile/),
      expect.stringMatching(/docs\/billing\.md.*hunk 1.*judged mechanical by the agent/),
    ]);
    await userEvent.click(within(items[1] as HTMLElement).getByRole("button", { name: "Show" }));
    expect(within(page).getByText("+the").tagName).toBe("PRE");
    expect(within(items[1] as HTMLElement).getByRole("button", { name: "Hide" })).toBeInTheDocument();
  });

  it("Walk everything asks for steps for the rest", async () => {
    const { calls } = setup(reviewed(), {
      [`POST ${API}/open`]: async (req: Request) => {
        const body = (await req.clone().text()) || "{}";
        const walk = (JSON.parse(body) as { walk_everything?: boolean }).walk_everything;
        return walk
          ? Response.json(
              { state: "started", reason: "reviewing", round_id: 3, jobs: ["walkthrough"] },
              { status: 202 },
            )
          : Response.json(UP_TO_DATE);
      },
    });
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Not walked through/);
    await userEvent.click(screen.getByRole("button", { name: "Walk everything" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`)).toHaveLength(2));
    const walk = sent(calls, "POST", `${API}/open`)[1];
    expect(await walk?.clone().json()).toEqual({ walk_everything: true, quiz: false });
    expect(walk?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(walk?.headers.get("Idempotency-Key")).not.toBe(
      sent(calls, "POST", `${API}/open`)[0]?.headers.get("Idempotency-Key"),
    );
    expect(await screen.findByText(/Walking everything: the new steps appear here/)).toBeInTheDocument();
  });

  it("says Nothing to walk when the diff changes no line, never 100%", async () => {
    const coverage = { ...ARTIFACTS.coverage, pct: 100, nothing_to_walk: true };
    setup(reviewed({ nothing_to_walk: true, artifacts: { ...ARTIFACTS, coverage } }, { nothing_to_walk: true }));
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    expect(within(toolbar()).getByText("Nothing to walk")).toBeInTheDocument();
    expect(within(toolbar()).queryByText(/100%/)).toBeNull();
    expect(within(outline()).getByText(/Walkthrough · nothing to walk/)).toBeInTheDocument();
  });

  it("says when the diff isn't cached yet, or GitHub doesn't show a file", async () => {
    setup(reviewed({ files: null }));
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/1\. The new prorate function/);
    expect(screen.getByText(/The diff isn't cached yet/)).toBeInTheDocument();
  });
});

describe("quick and deep", () => {
  const quick = () => {
    const { background, metrics, quiz, ...rest } = ARTIFACTS;
    void background;
    void metrics;
    void quiz;
    return reviewed({ round: { ...ROUND, depth: "quick" }, artifacts: rest }, { depth: "quick" });
  };

  it("a quick review has the short outline; the depth switches either way through /open", async () => {
    const { calls } = setup(quick());
    await screen.findByRole("heading", { level: 2, name: "Explanation" });
    const nav = outline();
    expect(within(nav).queryByRole("link", { name: /Background/ })).toBeNull();
    expect(within(nav).queryByRole("link", { name: /Metrics/ })).toBeNull();
    expect(within(nav).queryByRole("link", { name: /Quiz/ })).toBeNull();
    for (const entry of [/Explanation/, /1\. The new prorate function/, /Findings/, /Recommendation/]) {
      expect(within(nav).getByRole("link", { name: entry })).toBeInTheDocument();
    }
    expect(within(toolbar()).getByText("Quick")).toBeInTheDocument();
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Switch to deep" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`)).toHaveLength(2));
    expect(await sent(calls, "POST", `${API}/open`)[1]?.clone().json()).toEqual({
      depth: "deep",
      walk_everything: false,
      quiz: false,
    });
    expect(within(outline()).getByRole("link", { name: /Background.*not generated yet/ })).toBeInTheDocument();
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Switch to quick" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`)).toHaveLength(3));
    expect(await sent(calls, "POST", `${API}/open`)[2]?.clone().json()).toEqual({
      depth: "quick",
      walk_everything: false,
      quiz: false,
    });
    expect(within(outline()).queryByRole("link", { name: /Background/ })).toBeNull();
  });
});

describe("the quiz", () => {
  it("asks one question at a time, with feedback, and can be skipped", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Quiz/);
    const quiz = screen.getByRole("article", { name: "Quiz" });
    expect(within(quiz).getByText("Question 1 of 3")).toBeInTheDocument();
    expect(within(quiz).getByRole("group", { name: "When is the change charged?" })).toBeInTheDocument();
    expect(within(quiz).queryByText("What rounds?")).toBeNull();
    await userEvent.click(within(quiz).getByRole("button", { name: "Skip" }));
    expect(within(quiz).getByText("Question 2 of 3")).toBeInTheDocument();
    await userEvent.click(within(quiz).getByRole("radio", { name: "Each side" }));
    await userEvent.click(within(quiz).getByRole("button", { name: "Check" }));
    expect(within(quiz).getByText(/Not quite/)).toBeInTheDocument();
    expect(within(quiz).getByText(/Once, on the difference\./)).toBeInTheDocument();
    await userEvent.click(within(quiz).getByRole("button", { name: "Next question" }));
    await userEvent.click(within(quiz).getByRole("radio", { name: "The ledger" }));
    await userEvent.click(within(quiz).getByRole("button", { name: "Check" }));
    expect(within(quiz).getByText(/Right/)).toBeInTheDocument();
    await userEvent.click(within(quiz).getByRole("button", { name: "Finish" }));
    expect(within(quiz).getByText("1 of 3 right · 1 skipped. The quiz never blocks posting.")).toBeInTheDocument();
    await userEvent.click(within(quiz).getByRole("button", { name: "Start again" }));
    expect(within(quiz).getByText("Question 1 of 3")).toBeInTheDocument();
  });
});

describe("the quiz result", () => {
  const finishQuiz = async () => {
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Quiz/);
    const quiz = screen.getByRole("article", { name: "Quiz" });
    await userEvent.click(within(quiz).getByRole("button", { name: "Skip" }));
    await userEvent.click(within(quiz).getByRole("radio", { name: "Each side" }));
    await userEvent.click(within(quiz).getByRole("button", { name: "Check" }));
    await userEvent.click(within(quiz).getByRole("button", { name: "Next question" }));
    await userEvent.click(within(quiz).getByRole("radio", { name: "The ledger" }));
    await userEvent.click(within(quiz).getByRole("button", { name: "Check" }));
    await userEvent.click(within(quiz).getByRole("button", { name: "Finish" }));
    return quiz;
  };

  it("is recorded for the knowledge model once the quiz is finished (skips don't count)", async () => {
    const { calls } = setup(reviewed(), { [`POST ${API}/quiz`]: () => Response.json({ recorded: true }) });
    await finishQuiz();
    await waitFor(() => expect(sent(calls, "POST", `${API}/quiz`)).toHaveLength(1));
    const post = sent(calls, "POST", `${API}/quiz`)[0] as Request;
    expect(post.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await post.clone().json()).toEqual({ round_id: ROUND.id, right: 1, answered: 2 });
  });

  it("a failed recording never gets in the way of the quiz", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { calls } = setup(reviewed(), {
      [`POST ${API}/quiz`]: () => Response.json({ error: "Boom", detail: "down" }, { status: 503 }),
    });
    const quiz = await finishQuiz();
    await waitFor(() => expect(sent(calls, "POST", `${API}/quiz`)).toHaveLength(1));
    expect(within(quiz).getByText("1 of 3 right · 1 skipped. The quiz never blocks posting.")).toBeInTheDocument();
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(screen.queryByText(/quiz result/i)).toBeNull(); // nothing shown to the author
  });

  it("a quiz with every question skipped records nothing", async () => {
    const { calls } = setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Quiz/);
    const quiz = screen.getByRole("article", { name: "Quiz" });
    for (let i = 0; i < 3; i += 1) await userEvent.click(within(quiz).getByRole("button", { name: "Skip" }));
    expect(within(quiz).getByText(/0 of 3 right · 3 skipped/)).toBeInTheDocument();
    expect(sent(calls, "POST", `${API}/quiz`)).toHaveLength(0);
  });
});

describe("the other pages", () => {
  it("findings stay in their groups, with severity, location and rule", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Findings/);
    const page = screen.getByRole("article", { name: "Findings" });
    expect(within(page).getByRole("heading", { name: /Correctness · 1/ })).toBeInTheDocument();
    expect(within(page).getByRole("heading", { name: /Design · 0/ })).toBeInTheDocument();
    expect(within(page).getByRole("heading", { name: /Style · 1/ })).toBeInTheDocument();
    expect(within(page).getByText("src/billing/proration.py:14–15")).toBeInTheDocument();
    expect(within(page).getByText("python.md › Money · base")).toBeInTheDocument();
    expect(within(page).getByText("prorate_amount").tagName).toBe("CODE");
  });

  it("background, metrics and the recommendation's evidence", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await go(/Background/);
    expect(screen.getByRole("heading", { level: 3, name: "Billing today" })).toBeInTheDocument();
    expect(screen.getByText("src/billing/")).toBeInTheDocument();
    await go(/Metrics/);
    expect(screen.getByRole("cell", { name: "Error handling" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Error handling: 7 of 10" })).toBeInTheDocument();
    await go(/Recommendation/);
    const page = screen.getByRole("article", { name: "Recommendation" });
    expect(within(page).getByText("? Needs a closer look")).toBeInTheDocument();
    expect(within(page).getByText("the correctness pass found 1 important issue")).toBeInTheDocument();
    expect(within(page).getByText(/ci \/ test: passing/)).toBeInTheDocument();
    expect(within(page).getByText(/1 test covers the change/)).toBeInTheDocument();
    expect(within(page).getByText(/Correctness pass read 90% of changed lines/)).toBeInTheDocument();
    expect(within(page).getByText(/Walkthrough covers 82%/)).toBeInTheDocument();
    expect(within(page).getByText("One important finding.")).toBeInTheDocument();
  });

  it("says which jobs are running or failed, and Retry runs them again", async () => {
    const jobs = {
      ...ARTIFACTS.jobs,
      metrics: { state: "failed", error: "the agent timed out" },
      quiz: { state: "running", error: null },
    };
    const { metrics, quiz, ...rest } = ARTIFACTS;
    void metrics;
    void quiz;
    const { calls } = setup(reviewed({ artifacts: { ...rest, jobs } }), {
      [`POST ${API}/retry`]: () => Response.json({ state: "started", reason: "reviewing", round_id: 3, jobs: ["metrics"] }, { status: 202 }),
    }); // prettier-ignore
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    expect(within(outline()).getByRole("link", { name: /Quiz.*running/ })).toBeInTheDocument();
    await go(/Metrics.*failed/);
    expect(screen.getByText("Metrics failed: the agent timed out.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/retry`)).toHaveLength(1));
  });

  it("a gate holding the PR says why, and offers Review anyway", async () => {
    const { calls } = setup(reviewed({ round: null, artifacts: {}, files: null }), {
      [`POST ${API}/open`]: () =>
        Response.json({
          state: "ci_failing",
          reason: "CI is failing: Review anyway to review it",
          round_id: null,
          jobs: [],
        }),
      [`POST ${API}/review-anyway`]: () =>
        Response.json({ state: "started", reason: "reviewing", round_id: 4, jobs: ["correctness"] }, { status: 202 }),
      [`GET ${API}/drafts`]: () => Response.json({ error: "NoReviewRound", detail: "no round" }, { status: 404 }),
    });
    expect(await screen.findByText(/CI is failing: Review anyway to review it/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review anyway" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/review-anyway`)).toHaveLength(1));
    expect(await screen.findByText(/Reviewing: 1 job running/)).toBeInTheDocument();
  });

  it("marks a stale review and the last failure", async () => {
    setup(reviewed({ stale: true, round: { ...ROUND, error: "the agent timed out" } }));
    expect(await screen.findByText(/Stale: this PR has newer commits than this review/)).toBeInTheDocument();
    expect(screen.getByText("The last review failed: the agent timed out")).toBeInTheDocument();
  });
});

describe("chat", () => {
  it("Ask opens the shared chat rail, which starts a review session on the first message", async () => {
    const { calls } = setup(reviewed(), {
      "POST /api/agents/sessions": () =>
        Response.json({ id: "s1", subject: { kind: "review", ref: "acme/web#3", title: "t" } }, { status: 201 }),
      "POST /api/agents/sessions/s1/messages": () => Response.json({ ok: true }, { status: 202 }),
    });
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await userEvent.click(within(toolbar()).getByRole("button", { name: /Ask/ }));
    const rail = await screen.findByRole("complementary", { name: "Ask about this PR" });
    await userEvent.type(within(rail).getByRole("textbox", { name: "Message" }), "Why prorate?");
    await userEvent.click(within(rail).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sent(calls, "POST", "/api/agents/sessions")).toHaveLength(1));
    const body = (await sent(calls, "POST", "/api/agents/sessions")[0]?.clone().json()) as {
      subject: { kind: string; ref: string };
      untrusted: Record<string, { text: string }>;
    };
    expect(body.subject).toMatchObject({ kind: "review", ref: "acme/web#3" });
    expect(body.untrusted["review.md"]?.text).toContain("Posting to the ledger");
    await waitFor(() => expect(sent(calls, "POST", "/api/agents/sessions/s1/messages")).toHaveLength(1));
  });
});

void REVIEWED;
