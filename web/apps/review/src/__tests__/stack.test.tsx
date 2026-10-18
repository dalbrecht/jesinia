// SPDX-License-Identifier: AGPL-3.0-or-later
// Stacks and your own PRs (plan Task 13, spec §3.3, §4.3): the stack view's tabs (one Overview
// and Background, the stack walkthrough, a tab per PR with cross-PR findings called out), Post
// all's one confirmation and its stop-at-the-first-failure report, and your own PR's quiz.
import type { components } from "@jesinia/client";
import { fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import type { PrView, QueueRow, StackView } from "../api";
import { proposedEvent } from "../pr/StackView";
import { prView, queue, routes, row } from "./fixtures";
import { ARTIFACTS, drafts, preview, ROUND } from "./pr-fixtures";

type Outcome = components["schemas"]["PostOutcomeOut"];

// Built at run time, so this file holds no bidi character itself.
const RLO = String.fromCharCode(0x202e);
const STACK = "/api/review/stacks/acme/web/2";
const prApi = (n: number) => `/api/review/prs/acme/web/${n}`;

function member(n: number, over: Partial<QueueRow> = {}): PrView {
  const r = row(n, { stack: [1, 2, 3], kind: "stack", depth: "deep", head_sha: String(n).repeat(40), ...over });
  const steps = [{ title: `Step of #${n}`, body: `Walks #${n}.`, hunks: [{ path: "src/app.py", hunk: 0 }] }];
  return prView(r, {
    round: { ...ROUND, id: 10 + n, verdict: r.recommendation },
    artifacts: {
      ...ARTIFACTS,
      walkthrough: { steps, skipped: [] },
      verdict: { verdict: r.recommendation, reasons: [] },
    },
  });
}

const MEMBERS = [
  member(1, { recommendation: "approve" }),
  member(2, { recommendation: "needs_closer_look", author_kind: "you", title: `Own ${RLO}change` }),
  member(3, { recommendation: "request_changes" }),
];

function stackView(over: Partial<StackView> = {}): StackView {
  return {
    repo: "acme/web",
    signature: "1@1,2@2,3@3",
    members: MEMBERS,
    stack_id: 4,
    artifacts: {
      overview: { summary: "One change across **three** PRs.", purpose: "Split for review.", sources: [] },
      background: { sections: [{ title: "Billing today", body: "Plans change at renewal.", code: [], kb: [] }] },
      stack_walkthrough: {
        prs: [1, 2, 3].map((n) => ({ number: n, summary: `PR ${n} in one line.` })),
        steps: [
          {
            title: `Across ${RLO}the stack`,
            body: "From the model up.",
            hunks: [
              { pr: 1, path: "src/app.py", hunk: 0 },
              { pr: 3, path: `src/a${RLO}b.py`, hunk: 1 },
            ],
          },
        ],
        skipped: [
          { pr: 2, path: "uv.lock", hunk: 0, reason: "lockfile" },
          { pr: 3, path: "vendor/", hunk: null, reason: "vendored" },
        ],
        cross_findings: [],
      },
      jobs: { overview: { state: "done", error: null } },
    },
    cross_findings: [
      {
        prs: [1, 3],
        group: "correctness",
        severity: "important",
        explanation: "#3 relies on #1's default.",
        source: "agent",
        found_in: null,
        path: null,
        start_line: null,
        end_line: null,
        note: "across #1, #3",
      },
      {
        prs: [1, 3],
        group: "design",
        severity: "minor",
        explanation: "Duplicated helper.",
        source: "shared_file",
        found_in: 1,
        path: "src/app.py",
        start_line: 4,
        end_line: 4,
        note: "found in #1; src/app.py is also changed by #3",
      },
    ],
    ...over,
  };
}

function outcome(n: number, state: Outcome["state"], over: Partial<Outcome> = {}): Outcome {
  const event = n === 1 ? "APPROVE" : n === 3 ? "REQUEST_CHANGES" : "COMMENT";
  return { number: n, round_id: 10 + n, event, state, review_url: null, error: null, error_code: null, ...over };
}

function openStackOf(over: Routes = {}) {
  window.history.replaceState(null, "", "/review/acme/web/2");
  const view = MEMBERS[1] as PrView;
  const env = fakeClient(
    routes(queue(MEMBERS.map((m) => m.pr)), {
      [`GET ${prApi(2)}`]: () => Response.json(view),
      [`GET ${prApi(2)}/drafts`]: () => Response.json(drafts({ round_id: 12 })),
      [`GET ${STACK}`]: () => Response.json(stackView()),
      [`POST ${STACK}/open`]: () =>
        Response.json({
          state: "started",
          reason: "reviewing the stack",
          stack_id: 4,
          jobs: ["overview", "background", "stack_walkthrough"],
          members: [],
        }),
      ...Object.fromEntries(
        [1, 2, 3].map((n) => [
          `POST ${prApi(n)}/post/preview`,
          async (req: Request) => {
            const { event } = (await req.json()) as { event: string };
            return Response.json(preview({ round_id: 10 + n, event: event as never, digest: `d${n}-${event}` }));
          },
        ]),
      ),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

/** Text that `<Visible>` splits into spans, matched on the innermost element holding all of it. */
const whole = (text: string) => (_: string, el: Element | null) =>
  el?.textContent === text && ![...el.children].some((c) => c.textContent === text);

const toStack = async () => {
  await userEvent.click(await screen.findByRole("button", { name: "Review the stack of 3" }));
  return screen.findByRole("tablist", { name: "Stack review" });
};

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/review");
});

describe("the stack view", () => {
  it("has one Overview and Background, the stack walkthrough and a tab per PR", async () => {
    const env = openStackOf();
    const tabs = await toStack();
    const names = within(tabs)
      .getAllByRole("tab")
      .map((t) => t.textContent);
    expect(names).toEqual([
      "Overview",
      "Background",
      "Stack walkthrough",
      "#1 PR 1",
      "#2 Own ⟪U+202E⟫change",
      "#3 PR 3",
      "Cross-PR findings2",
    ]);
    expect(await screen.findByText(/Reviewing the stack: 3 jobs running/)).toBeInTheDocument();
    // Opened once for the stack, keyed.
    await waitFor(() => expect(env.calls.filter((c) => new URL(c.url).pathname === `${STACK}/open`)).toHaveLength(1));
    const opened = env.calls.find((c) => new URL(c.url).pathname === `${STACK}/open`);
    expect(opened?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
    expect(screen.getByText("One overview for #1, #2, #3")).toBeInTheDocument();

    await userEvent.click(within(tabs).getByRole("tab", { name: "Stack walkthrough" }));
    // R62: agent step titles show their hidden characters as visible tokens.
    expect(await screen.findByText(whole("1. Across ⟪U+202E⟫the stack"))).toBeInTheDocument();
    expect(screen.getByText(whole("#3 src/a⟪U+202E⟫b.py · hunk 2"))).toBeInTheDocument();
    expect(screen.getByText(whole("#3 vendor/: vendored"))).toBeInTheDocument();
    expect(screen.getByText("#2 uv.lock · hunk 1: lockfile")).toBeInTheDocument();

    await userEvent.click(within(tabs).getByRole("tab", { name: "#3 PR 3" }));
    const page = await screen.findByRole("article", { name: "#3" });
    expect(within(page).getByRole("heading", { name: "1. Step of #3" })).toBeInTheDocument();
    expect(within(page).getByText("✕ Request changes")).toBeInTheDocument();
    const callout = within(page).getByRole("region", { name: "Cross-PR findings" });
    expect(within(callout).getByText(/2 findings concern other PRs/)).toBeInTheDocument();
    expect(within(callout).getByText(/across #1, #3/)).toBeInTheDocument();
    expect(within(callout).getByText(whole("src/app.py:4"))).toBeInTheDocument();
    // #2 isn't named by the cross-PR finding: no callout there; it is your own PR.
    await userEvent.click(within(tabs).getByRole("tab", { name: /#2 Own/ }));
    const own = await screen.findByRole("article", { name: "#2" });
    expect(within(own).queryByRole("region", { name: "Cross-PR findings" })).toBeNull();
    expect(within(own).getByText("? Not ready to request review: it needs a closer look")).toBeInTheDocument();

    await userEvent.click(within(tabs).getByRole("tab", { name: /Cross-PR findings/ }));
    expect(await screen.findByRole("heading", { level: 2, name: "Cross-PR findings" })).toBeInTheDocument();

    // Another workspace tab and back: the stack stays where it was (R60).
    await userEvent.click(screen.getByRole("link", { name: /Queue/ }));
    await userEvent.click(screen.getByRole("link", { name: /Own/ }));
    expect(await screen.findByRole("heading", { level: 2, name: "Cross-PR findings" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "← This PR only" }));
    expect(await screen.findByRole("navigation", { name: "Review outline" })).toBeInTheDocument();
  });

  it("proposes each PR's action from its verdict", () => {
    expect(proposedEvent(member(4, { recommendation: null }))).toBe("COMMENT");
    const approved = member(5, { recommendation: null });
    expect(proposedEvent({ ...approved, round: { ...ROUND, verdict: "approve" } })).toBe("APPROVE");
    expect(proposedEvent({ ...approved, round: null })).toBe("COMMENT");
  });

  it("shows an all-own stack minimised, and what isn't generated yet", async () => {
    const mine = MEMBERS.map((m, i) =>
      prView({ ...m.pr, author_kind: "you", recommendation: i === 0 ? null : m.pr.recommendation }, { round: m.round }),
    );
    const view = stackView({
      members: mine,
      artifacts: { jobs: { stack_walkthrough: { state: "failed", error: null } } },
      cross_findings: [],
    });
    const env = openStackOf({
      [`GET ${STACK}`]: () => Response.json(view),
      [`POST ${STACK}/open`]: () =>
        Response.json({ state: "up_to_date", reason: "complete", stack_id: 4, jobs: [], members: [] }),
    });
    const tabs = await toStack();
    expect(await screen.findByText("The stack's overview hasn't been generated yet.")).toBeInTheDocument();
    const banner = await screen.findByText("The stack walkthrough failed.");
    await userEvent.click(within(banner.parentElement as HTMLElement).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(env.calls.filter((c) => new URL(c.url).pathname === `${STACK}/open`)).toHaveLength(2));
    await userEvent.click(within(tabs).getByRole("tab", { name: "Background · minimised" }));
    expect(await screen.findByText(/You wrote this stack/)).toBeInTheDocument();
    await userEvent.click(within(tabs).getByRole("tab", { name: "Stack walkthrough" }));
    expect(await screen.findByText("The stack walkthrough failed: no cause given")).toBeInTheDocument();
    await userEvent.click(within(tabs).getByRole("tab", { name: "#1 PR 1" }));
    const first = await screen.findByRole("article", { name: "#1" });
    expect(within(first).getByText("No verdict yet")).toBeInTheDocument();
    expect(within(first).getByText("#1's walkthrough: no steps yet.")).toBeInTheDocument();
    expect(within(first).getByText("Findings aren't in yet.")).toBeInTheDocument();
  });

  it("says why a stack couldn't be read or opened, and tries again", async () => {
    let reads = 0;
    openStackOf({
      [`GET ${STACK}`]: () => {
        reads += 1;
        return reads === 1
          ? Response.json({ error: "ReviewError", detail: "the store is busy" }, { status: 500 })
          : Response.json(stackView());
      },
      [`POST ${STACK}/open`]: () => Response.json({ error: "ReviewError", detail: "GitHub is down" }, { status: 503 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Review the stack of 3" }));
    expect(await screen.findByText(/Couldn't read this stack: the store is busy/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Couldn't start the stack's review: GitHub is down/)).toBeInTheDocument();
  });

  it("says what is still generating, and Retry runs a failed stack job again", async () => {
    const view = stackView({
      artifacts: {
        jobs: {
          overview: { state: "running", error: null },
          background: { state: "failed", error: `the agent fell ${RLO}over` },
          stack_walkthrough: { state: "interrupted", error: "Jesinia stopped during this job; Retry runs it again" },
        },
      },
      cross_findings: [],
    });
    const env = openStackOf({
      [`GET ${STACK}`]: () => Response.json(view),
      [`POST ${STACK}/open`]: async (req: Request) => {
        const { retry } = (await req.json()) as { retry: boolean };
        return Response.json(
          retry
            ? { state: "started", reason: "reviewing", stack_id: 4, jobs: ["background"], members: [] }
            : {
                state: "held",
                reason: "the stack's review waits for every PR: #3: CI is running",
                stack_id: 4,
                jobs: [],
                members: [],
              },
        );
      },
    });
    const tabs = await toStack();
    expect(await screen.findByText(/waits for every PR: #3: CI is running/)).toBeInTheDocument();
    expect(screen.getByText(/The stack's overview is being generated/)).toBeInTheDocument();
    const opens = () => env.calls.filter((c) => new URL(c.url).pathname === `${STACK}/open`);
    // Held: once CI passes, the author asks again (M4).
    await userEvent.click(screen.getByRole("button", { name: "Review the stack" }));
    await waitFor(() => expect(opens()).toHaveLength(2));
    await userEvent.click(within(tabs).getByRole("tab", { name: "Background" }));
    // R62 on job causes (M7).
    expect(await screen.findByText("The stack's background failed: the agent fell ⟪U+202E⟫over")).toBeInTheDocument();
    await userEvent.click(within(tabs).getByRole("tab", { name: "Stack walkthrough" }));
    // R73: cut short by a stop, it waits for Retry.
    expect(await screen.findByText(/The stack walkthrough was cut short: Jesinia stopped/)).toBeInTheDocument();
    await userEvent.click(
      within(screen.getByRole("article", { name: "Stack walkthrough" })).getByRole("button", { name: "Retry" }),
    );
    expect(await screen.findByText(/Reviewing the stack: 1 job running/)).toBeInTheDocument();
    expect(opens()).toHaveLength(3);
    // (The route answers "started" only to a retry: the body was { retry: true }.)
  });

  it("Post all lists each PR and its action, reports a stop, and finishes with the same key", async () => {
    let attempt = 0;
    const firstPosted = stackView({
      members: MEMBERS.map((m) =>
        m.pr.number === 1 ? { ...m, round: { ...(m.round as typeof ROUND), posted_review_url: "https://x/1" } } : m,
      ),
    });
    const env = openStackOf({
      [`GET ${STACK}`]: () => Response.json(attempt === 0 ? stackView() : firstPosted),
      [`POST ${STACK}/post-all`]: () => {
        attempt += 1;
        if (attempt === 1)
          return Response.json(
            {
              error: "PostingFailed",
              detail: "posting the review failed (GitHub's answer was lost)",
              stack_outcomes: [
                outcome(1, "posted", { review_url: "https://github.com/acme/web/pull/1#r1" }),
                outcome(2, "failed", { error: "GitHub's answer was lost", error_code: "PostingFailed" }),
                outcome(3, "not_attempted"),
              ],
            },
            { status: 502 },
          );
        return Response.json({ outcomes: [outcome(2, "posted"), outcome(3, "posted")] });
      },
    });
    await toStack();
    await userEvent.click(screen.getByRole("button", { name: "Post all…" }));
    const dialog = await screen.findByRole("dialog", { name: "Post all: 3 PRs on GitHub" });
    // Each PR with the action its verdict proposes; your own PR can only take a comment.
    expect(within(dialog).getByRole("combobox", { name: "Action for #1" })).toHaveValue("APPROVE");
    expect(within(dialog).getByRole("combobox", { name: "Action for #2" })).toHaveValue("COMMENT");
    expect(within(within(dialog).getByRole("combobox", { name: "Action for #2" })).getAllByRole("option")).toHaveLength(
      1,
    );
    expect(within(dialog).getByRole("combobox", { name: "Action for #3" })).toHaveValue("REQUEST_CHANGES");
    expect(within(dialog).getByText(whole("#2 Own ⟪U+202E⟫change"))).toBeInTheDocument();
    await userEvent.selectOptions(within(dialog).getByRole("combobox", { name: "Action for #3" }), "COMMENT");
    const post = within(dialog).getByRole("button", { name: "Post all (3)" });
    await waitFor(() => expect(post).toBeEnabled());
    await userEvent.click(post);

    const results = await within(dialog).findByRole("list", { name: "Post all results" });
    expect(within(results).getByText(/#1 Approve: posted/)).toBeInTheDocument();
    expect(within(results).getByText(/#2 Comment: failed/)).toBeInTheDocument();
    expect(within(results).getByText(/#3 Request changes: not attempted/)).toBeInTheDocument();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/Post all stopped.*nothing is posted twice/);

    const posts = () => env.calls.filter((c) => new URL(c.url).pathname === `${STACK}/post-all`);
    const first = posts()[0] as Request;
    const sent = (await first.clone().json()) as { items: { number: number; event: string; digest: string }[] };
    expect(sent.items.map((i) => [i.number, i.event, i.digest])).toEqual([
      [1, "APPROVE", "d1-APPROVE"],
      [2, "COMMENT", "d2-COMMENT"],
      [3, "COMMENT", "d3-COMMENT"],
    ]);

    // Check again lists the stack as it is now: #1 is posted, so it is left out, and says so.
    await userEvent.click(within(dialog).getByRole("button", { name: "Check again" }));
    const again = await within(dialog).findByRole("button", { name: "Post all (2)" });
    expect(within(dialog).getByText("#1: posted already")).toBeInTheDocument();
    await waitFor(() => expect(again).toBeEnabled());
    await userEvent.click(again);
    const done = await within(dialog).findByRole("list", { name: "Post all results" });
    await waitFor(() => expect(within(done).getAllByText(/: posted/)).toHaveLength(2));
    // The lost answer's retry is the same request: one key, so nothing posts twice.
    expect(posts()).toHaveLength(2);
    expect(posts()[1]?.headers.get("Idempotency-Key")).toBe(first.headers.get("Idempotency-Key"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("leaves PRs it can't post out of Post all, saying why", async () => {
    const posted = stackView({
      members: MEMBERS.map((m) =>
        m.pr.number === 3
          ? m
          : m.pr.number === 2
            ? { ...m, round: null }
            : { ...m, round: { ...(m.round as typeof ROUND), frozen: true } },
      ),
      artifacts: {},
      cross_findings: [],
    });
    openStackOf({
      [`GET ${STACK}`]: () => Response.json(posted),
      [`POST ${STACK}/open`]: () =>
        Response.json({ state: "running", reason: "running", stack_id: 4, jobs: [], members: [] }),
    });
    const tabs = await toStack();
    expect(await screen.findByText(/The stack is being reviewed/)).toBeInTheDocument();
    await userEvent.click(within(tabs).getByRole("tab", { name: /Cross-PR findings/ }));
    expect(await screen.findByText("No cross-PR findings.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Post all…" }));
    const dialog = await screen.findByRole("dialog", { name: "Post all: 1 PR on GitHub" });
    expect(within(dialog).queryByRole("combobox", { name: "Action for #1" })).toBeNull();
    expect(within(dialog).getByText("#1: moved on since this review; review its new head first")).toBeInTheDocument();
    expect(within(dialog).getByText("#2: no review yet")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("reads each preview again after a failure, and says when Jesinia didn't answer", async () => {
    let previews = 0;
    openStackOf({
      [`POST ${prApi(3)}/post/preview`]: () => {
        previews += 1;
        return previews === 1
          ? Response.json({ error: "GitHubError", detail: "GitHub is slow" }, { status: 502 })
          : Response.json(preview({ round_id: 13, event: "REQUEST_CHANGES", comments: [], body: "", digest: "d3" }));
      },
      [`POST ${prApi(1)}/post/preview`]: () =>
        Response.json(preview({ round_id: 11, warnings: ["approve_against_evidence"], digest: "d1" })),
      [`POST ${STACK}/post-all`]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    await toStack();
    await userEvent.click(screen.getByRole("button", { name: "Post all…" }));
    const dialog = await screen.findByRole("dialog", { name: "Post all: 3 PRs on GitHub" });
    expect(await within(dialog).findByText(/Couldn't read what would be posted: GitHub is slow/)).toBeInTheDocument();
    expect(within(dialog).getByText("#1: Jesinia's verdict isn't Approve")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Post all (3)" })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await within(dialog).findByText("0 inline comments")).toBeInTheDocument();
    // I2: every preview is in, but #1's warning isn't confirmed: nothing can be posted yet.
    const post = within(dialog).getByRole("button", { name: "Post all (3)" });
    expect(post).toBeDisabled();
    const box = within(dialog).getByRole("checkbox", { name: "Approve #1 anyway" });
    await userEvent.click(box);
    await waitFor(() => expect(post).toBeEnabled());
    await userEvent.click(box); // unticked again: locked again
    expect(post).toBeDisabled();
    await userEvent.click(box);
    await waitFor(() => expect(post).toBeEnabled());
    await userEvent.click(post);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Couldn't post: Jesinia didn't answer/);
  });
});

describe("long titles", () => {
  it("truncates a stack tab's title and shows all of it on hover", async () => {
    const long = `Refactor the billing ${RLO}ledger so that proration happens once per change, not per line`;
    const members = MEMBERS.map((m) => (m.pr.number === 3 ? { ...m, pr: { ...m.pr, title: long } } : m));
    openStackOf({ [`GET ${STACK}`]: () => Response.json(stackView({ members })) });
    const tabs = await toStack();
    const tab = within(tabs).getByRole("tab", { name: /^#3 / });
    expect(tab.textContent?.endsWith("…")).toBe(true);
    expect((tab.textContent ?? "").length).toBeLessThanOrEqual(44);
    expect(tab).toHaveAttribute("title", `#3 ${long.replace(RLO, "⟪U+202E⟫")}`);
  });
});

describe("your own PR", () => {
  it("reads as ready (or not) to request review on its queue row", async () => {
    const env = fakeClient(routes(queue([row(9, { author_kind: "you", recommendation: "approve" })])));
    render(<App client={env.client} />);
    const card = await screen.findByRole("article", { name: /#9/ });
    expect(within(card).getByText("✓ Ready to request review")).toBeInTheDocument();
    expect(within(card).queryByText("✓ Approve")).toBeNull();
  });

  it("has its quiz off until asked for, and its background minimised", async () => {
    const own = prView(row(7, { author_kind: "you", depth: "deep", recommendation: "approve" }), {
      round: { ...ROUND, id: 70 },
      artifacts: { ...ARTIFACTS, quiz: undefined, jobs: { overview: { state: "done", error: null } } },
    });
    window.history.replaceState(null, "", "/review/acme/web/7");
    const env = fakeClient(
      routes(queue([own.pr]), {
        [`GET ${prApi(7)}`]: () => Response.json(own),
        [`GET ${prApi(7)}/drafts`]: () => Response.json(drafts({ round_id: 70 })),
      }),
    );
    render(<App client={env.client} />);
    const outline = await screen.findByRole("navigation", { name: "Review outline" });
    expect(within(outline).getByRole("link", { name: /Background · minimised/ })).toBeInTheDocument();
    await userEvent.click(within(outline).getByRole("link", { name: /Quiz.*off for your own PR/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Generate a quiz anyway" }));
    await waitFor(() =>
      expect(
        env.calls.some((c) => c.method === "POST" && new URL(c.url).pathname === `${prApi(7)}/open` && c.url),
      ).toBe(true),
    );
    const asked = env.calls.filter((c) => c.method === "POST" && new URL(c.url).pathname === `${prApi(7)}/open`);
    const bodies = await Promise.all(asked.map((c) => c.clone().text()));
    expect(bodies.some((b) => b.includes('"quiz":true'))).toBe(true);
    // The verdict reads as ready (or not) to request review.
    expect(screen.getAllByText("? Not ready to request review: it needs a closer look").length).toBeGreaterThan(0);
  });
});

describe("a member leaving the stack (Codex round 2 P2 on PR #63)", () => {
  it("falls back to the Overview when the selected PR's tab goes away", async () => {
    let reads = 0;
    const twoLeft = stackView({ members: MEMBERS.slice(0, 2), signature: "1@1,2@2" });
    openStackOf({
      [`GET ${STACK}`]: () => {
        reads += 1;
        return Response.json(reads < 3 ? stackView() : twoLeft);
      },
      [`POST ${STACK}/open`]: () =>
        Response.json({ state: "held", reason: "waiting for CI on #3", stack_id: 4, jobs: [], members: [] }),
    });
    const tabs = await toStack();
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2)); // read, opened, read again
    await userEvent.click(within(tabs).getByRole("tab", { name: /^#3/ }));
    await userEvent.click(screen.getByRole("button", { name: "Review the stack" })); // reads again
    await waitFor(() =>
      expect(
        within(screen.getByRole("tablist", { name: "Stack review" })).queryByRole("tab", { name: /^#3/ }),
      ).toBeNull(),
    );
    expect(
      within(screen.getByRole("tablist", { name: "Stack review" })).getByRole("tab", { name: "Overview" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
  });
});
